from __future__ import annotations

import json
import socket
import subprocess
import sys
import unittest
import urllib.request
from unittest.mock import Mock, patch

from ocsl_review.confidentiality import EgressPlan, authorize_egress
from ocsl_review.errors import ConfidentialityError, ProviderError
from ocsl_review.fulltext import (
    MAX_DOCUMENT_BYTES,
    _PublicHTTPSConnection,
    _Response,
    _extract_pdf_text,
    open_public_https,
    parse_source_document,
    validate_public_url,
)
from ocsl_review.scholarly import (
    MAX_RESPONSE_BYTES,
    STATUS_POSITIVE_CONTROLS,
    RawResponse,
    ScholarlyClient,
    normalize_crossref,
    normalize_europe_pmc,
    parse_pubmed_record,
)


def article_xml() -> bytes:
    sections = "".join(f'<sec id="s{number}"><title>Section {number}</title><p>{"x" * 4100} Decisive finding {number}.</p></sec>' for number in range(1, 23))
    return (f'<article><front><article-meta><article-title>Sample study</article-title></article-meta></front><body>{sections}'
            '<table-wrap id="t1"><label>Table 1</label><caption><p>Measured response.</p></caption><table><tbody><tr><th>Group</th><th>Response</th></tr><tr><td>Exposed</td><td>42</td></tr></tbody></table><table-wrap-foot><p>Values use the enrolled denominator.</p></table-wrap-foot></table-wrap>'
            '<fig id="f1"><label>Figure 1</label><caption><p>Uncertainty interval.</p></caption></fig>'
            '<supplementary-material xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="supplement.pdf"><label>Protocol</label></supplementary-material>'
            '</body><back><ack><p>Data stewardship statement.</p></ack></back></article>').encode()


def pubmed_xml(pmid: str, notices=(), abstract="An observed result.") -> bytes:
    notice_xml = "".join(f'<CommentsCorrections RefType="{kind}"><RefSource>Notice journal</RefSource><PMID>{identifier}</PMID></CommentsCorrections>' for kind, identifier in notices)
    return (f'<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><ArticleTitle>Sample study</ArticleTitle>'
            '<Journal><Title>Example Journal</Title><JournalIssue><Volume>3</Volume><Issue>2</Issue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal>'
            f'<Abstract><AbstractText Label="RESULTS">{abstract}</AbstractText></Abstract><Pagination><MedlinePgn>15-20</MedlinePgn></Pagination></Article>'
            f'<CommentsCorrectionsList>{notice_xml}</CommentsCorrectionsList></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1234/example</ArticleId></ArticleIdList></PubmedData>'
            '</PubmedArticle></PubmedArticleSet>').encode()


class SourceTransport:
    def __init__(self, *, xml=None, open_access=True, live_controls=True, notice=False, abstract="An observed result."):
        self.xml = article_xml() if xml is None else xml
        self.open_access = open_access
        self.live_controls = live_controls
        self.notice = notice
        self.abstract = abstract
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, params or {}, headers or {}))
        if url.endswith("/fullTextXML") or url.startswith("https://papers.example/"):
            return RawResponse(self.xml, 200, {"Content-Type": "application/xml"}, url)
        if "efetch.fcgi" in url:
            pmid = params["id"]
            controls = {p: (r, n) for p, r, n in STATUS_POSITIVE_CONTROLS}
            notices = [controls[pmid]] if pmid in controls and self.live_controls else [("ErratumIn", "456")] if self.notice and pmid == "123" else []
            return RawResponse(pubmed_xml(pmid, notices, self.abstract), 200, {})
        if "esummary.fcgi" in url:
            data = {"result": {"123": {"title": "Sample study", "pubdate": "2024", "source": "Example Journal", "articleids": [{"idtype": "doi", "value": "10.1234/example"}]}}}
        elif "unpaywall" in url:
            data = {"doi": "10.1234/example", "best_oa_location": {"url_for_pdf": "https://papers.example/source.pdf", "version": "publishedVersion", "license": "cc-by"}}
        elif "crossref" in url:
            data = {"message": {"DOI": "10.1234/example", "title": ["Sample study"], "published": {"date-parts": [[2024]]}, "container-title": ["Example Journal"]}}
        elif "europepmc" in url:
            data = {"resultList": {"result": [{"id": "123", "source": "MED", "doi": "10.1234/example", "pmcid": "PMC123", "title": "Sample study", "pubYear": "2024", "journalTitle": "Example Journal", "abstractText": self.abstract, "isOpenAccess": "Y" if self.open_access else "N"}]}}
        else:
            raise AssertionError(url)
        return RawResponse(json.dumps(data).encode(), 200, {})


class FullTextTests(unittest.TestCase):
    def test_inline_supplement_is_inventoried_inside_a_paragraph(self):
        body = b'''<article xmlns:xlink="http://www.w3.org/1999/xlink"><body><p>
        See <supplementary-material xlink:href="supplement.pdf"><label>Protocol</label>
        </supplementary-material> for additional methods.</p></body></article>'''
        result = parse_source_document(body, "application/xml")
        self.assertEqual(result["supplements"], [{"label": "Protocol", "href": "supplement.pdf"}])
        self.assertIn("additional methods", result["text"])

    def test_complete_article_contains_late_long_passages_tables_captions_and_supplements(self):
        result = parse_source_document(article_xml(), "application/xml")
        self.assertTrue(result["complete"])
        self.assertIn("Decisive finding 22.", result["text"])
        self.assertIn("Exposed | 42", result["text"])
        self.assertIn("enrolled denominator", result["text"])
        self.assertIn("Uncertainty interval", result["text"])
        self.assertIn("Data stewardship", result["text"])
        self.assertEqual(result["supplements"], [{"label": "Protocol", "href": "supplement.pdf"}])
        self.assertEqual(len(result["blocks"]), len({block["id"] for block in result["blocks"]}))

    def test_empty_malformed_metadata_and_entity_documents_cannot_be_complete(self):
        for body in (b"", b"<article", b"<article><front>Abstract only</front></article>", b"<article><body/></article>", b"<!DOCTYPE article [<!ENTITY bad 'expanded'>]><article><body><p>&bad;</p></body></article>", b"<html><body>Login</body></html>"):
            with self.subTest(body=body), self.assertRaises(ProviderError):
                parse_source_document(body)

    def test_document_bound_is_distinct_from_metadata_bound(self):
        self.assertEqual(MAX_DOCUMENT_BYTES, 100 * 1024 * 1024)
        self.assertGreater(MAX_DOCUMENT_BYTES, MAX_RESPONSE_BYTES)
        with patch("ocsl_review.fulltext.MAX_DOCUMENT_BYTES", 20), self.assertRaisesRegex(ProviderError, "100 MiB"):
            parse_source_document(b"x" * 21, "text/plain")

    def test_pdf_empty_page_does_not_certify_complete_readable_text(self):
        with patch("ocsl_review.fulltext._extract_pdf_text", return_value=b"Page one\f\f"), self.assertRaisesRegex(ProviderError, "image-only"):
            parse_source_document(b"%PDF-1.4 synthetic", "application/pdf")

    def test_pdf_child_output_bound_stops_a_running_writer(self):
        original_popen = subprocess.Popen

        def child(*args, **kwargs):
            return original_popen([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*8192); sys.stdout.flush(); sys.stdin.buffer.read()"], **kwargs)

        with patch("ocsl_review.fulltext.subprocess.Popen", side_effect=child), patch("ocsl_review.fulltext.MAX_DOCUMENT_BYTES", 1024), self.assertRaisesRegex(ProviderError, "output bound"):
            _extract_pdf_text(b"synthetic document", 5)

    def test_acquire_keeps_full_source_bytes_and_complete_abstract(self):
        client = ScholarlyClient(transport=SourceTransport())
        result = client.acquire_full_text(doi="10.1234/example")
        self.assertEqual(result["accessLevel"], "full-text")
        self.assertTrue(result["abstractComplete"])
        self.assertEqual(result["documents"][0]["body"], article_xml())
        self.assertIn("Decisive finding 22.", result["documents"][0]["text"])

    def test_preview_is_explicitly_partial(self):
        result = ScholarlyClient(transport=SourceTransport()).get_abstract_or_open_sections(doi="10.1234/example")
        self.assertEqual(result["accessLevel"], "partial-text")
        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertNotIn("Decisive finding 22.", str(result["records"]))

    def test_failed_fulltext_retains_abstract_without_success(self):
        with patch.dict("os.environ", {}, clear=True):
            result = ScholarlyClient(transport=SourceTransport(xml=b"<error/>"), contact_email=None).acquire_full_text(doi="10.1234/example")
        self.assertEqual(result["documents"], [])
        self.assertEqual(result["accessLevel"], "abstract-only")
        self.assertEqual(result["attempts"][0]["status"], "failed")
        self.assertTrue(any("OCSL_REVIEW_CONTACT_EMAIL" in message for message in result["limitations"]))

    def test_unpaywall_uses_configured_contact_only_and_retains_version(self):
        transport = SourceTransport(open_access=False)
        result = ScholarlyClient(transport=transport, contact_email="operator@example.org").acquire_full_text(doi="10.1234/example")
        request = next(call for call in transport.calls if "unpaywall" in call[0])
        self.assertEqual(request[1], {"email": "operator@example.org"})
        self.assertEqual(result["documents"][0]["version"], "publishedVersion")
        paper_request = next(call for call in transport.calls if "papers.example" in call[0])
        self.assertNotIn("operator@example.org", str(paper_request))

    def test_acquisition_policy_gate_precedes_transport(self):
        transport = SourceTransport()
        plan = EgressPlan(approved_operations=frozenset({"resolve_identifier"}))
        with self.assertRaises(ConfidentialityError):
            ScholarlyClient(transport=transport, plan=plan).acquire_full_text(doi="10.1234/example")
        self.assertEqual(transport.calls, [])

    def test_delivered_xml_with_other_source_identifier_is_rejected(self):
        xml = article_xml().replace(b"<article-meta>", b'<article-meta><article-id pub-id-type="doi">10.1234/different</article-id>')
        with patch.dict("os.environ", {}, clear=True):
            result = ScholarlyClient(transport=SourceTransport(xml=xml)).acquire_full_text(doi="10.1234/example")
        self.assertEqual(result["documents"], [])
        self.assertIn("different source", result["attempts"][0]["error"])

    def test_invalid_delivered_doi_is_a_retryable_source_failure(self):
        xml = article_xml().replace(b"<article-meta>", b'<article-meta><article-id pub-id-type="doi">invalid</article-id>')
        with patch.dict("os.environ", {}, clear=True):
            result = ScholarlyClient(transport=SourceTransport(xml=xml)).acquire_full_text(doi="10.1234/example")
        self.assertEqual(result["documents"], [])
        self.assertIn("invalid source DOI", result["attempts"][0]["error"])

    def test_remote_plain_access_message_is_not_a_complete_document(self):
        transport = Mock()
        transport.get_document.return_value = RawResponse(b"Access denied.", 200, {"Content-Type": "text/plain"})
        with self.assertRaises(ProviderError):
            ScholarlyClient(transport=transport)._source_document("https://papers.example/file", {"doi": "10.1234/example"})


class MetadataAndNoticeTests(unittest.TestCase):
    def test_pmcid_resolves_through_bounded_independent_identifier_routes(self):
        transport = SourceTransport()
        client = ScholarlyClient(transport=transport)
        result = client.resolve_identifier(pmcid="pmc123")
        self.assertEqual([record["provider"] for record in result["records"]],
                         ["europe-pmc", "pubmed", "crossref"])
        self.assertEqual(len(transport.calls), 3)
        agreement = client.reconcile_reference_metadata(
            result["records"], {"raw": "Sample study. Example Journal. 2024. PMCID:PMC123"})
        self.assertEqual(agreement["agreementStrength"], "strong")
        acquired = client.acquire_full_text(pmcid="PMC123")
        self.assertEqual(acquired["accessLevel"], "full-text")
        self.assertEqual(acquired["documents"][0]["body"], article_xml())

    def test_pmcid_metadata_conflicts_survive_independent_provider_resolution(self):
        class ConflictingTransport(SourceTransport):
            def get(self, url, **kwargs):
                response = super().get(url, **kwargs)
                if "crossref" in url:
                    payload = response.json()
                    payload["message"]["title"] = ["A different study"]
                    return RawResponse(json.dumps(payload).encode(), 200, {})
                return response

        transport = ConflictingTransport()
        client = ScholarlyClient(transport=transport)
        result = client.resolve_identifier(pmcid="PMC123")
        self.assertEqual(result["records"][0]["title"], "Sample study")
        self.assertEqual(len(transport.calls), 3)
        agreement = client.reconcile_reference_metadata(
            result["records"], {"raw": "Sample study. Example Journal. 2024. PMCID:PMC123"})
        self.assertEqual(agreement["agreementStrength"], "conflict")

    def test_pmcid_can_discover_doi_through_pubmed_without_recursive_resolution(self):
        class NoDoiTransport(SourceTransport):
            def get(self, url, **kwargs):
                response = super().get(url, **kwargs)
                if "europepmc" in url:
                    payload = response.json()
                    payload["resultList"]["result"][0].pop("doi")
                    return RawResponse(json.dumps(payload).encode(), 200, {})
                return response

        transport = NoDoiTransport()
        result = ScholarlyClient(transport=transport).resolve_identifier(pmcid="PMC123")
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(result["records"][-1]["provider"], "crossref")
        self.assertEqual(result["records"][-1]["doi"], "10.1234/example")

    def test_pmcid_followups_retain_outer_operation_and_identifier_only_egress(self):
        transport = SourceTransport()
        plan = EgressPlan(approved_operations=frozenset({"resolve_reference"}))
        client = ScholarlyClient(plan=plan, transport=transport)
        with patch("ocsl_review.scholarly.authorize_egress", wraps=authorize_egress) as authorize:
            client.resolve_reference({"pmcid": "PMC123"})
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual({call.kwargs["operation"] for call in authorize.call_args_list},
                         {"resolve_reference"})
        self.assertEqual([call.kwargs["arguments"] for call in authorize.call_args_list],
                         [{"pmcid": "PMC123"}, {"pmid": "123"}, {"doi": "10.1234/example"}])
        with self.assertRaises(ConfidentialityError):
            client.resolve_identifier(pmcid="PMC123")
        self.assertEqual(len(transport.calls), 3)

    def test_mismatched_pmcid_record_does_not_seed_unrelated_identifier_requests(self):
        class WrongPmcidTransport(SourceTransport):
            def get(self, url, **kwargs):
                response = super().get(url, **kwargs)
                payload = response.json()
                payload["resultList"]["result"][0]["pmcid"] = "PMC999"
                return RawResponse(json.dumps(payload).encode(), 200, {})

        transport = WrongPmcidTransport()
        result = ScholarlyClient(transport=transport).resolve_identifier(pmcid="PMC123")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual([record["provider"] for record in result["records"]], ["europe-pmc"])

    def test_pmid_retains_independent_doi_route_when_europe_pmc_fails(self):
        class EuropePmcOutage(SourceTransport):
            def get(self, url, **kwargs):
                response = super().get(url, **kwargs)
                if "europepmc" in url:
                    raise ProviderError("synthetic Europe PMC outage")
                return response

        transport = EuropePmcOutage()
        client = ScholarlyClient(transport=transport)
        result = client.resolve_identifier(pmid="123")
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual([record["provider"] for record in result["records"]], ["pubmed", "crossref"])
        agreement = client.reconcile_reference_metadata(
            result["records"], {"raw": "Sample study. Example Journal. 2024. PMID:123"})
        self.assertEqual(agreement["agreementStrength"], "strong")
        self.assertTrue(any("synthetic Europe PMC outage" in item for item in result["limitations"]))

    def test_identifier_disagreement_cannot_hide_behind_matching_titles(self):
        first = {"provider": "pubmed", "pmid": "123", "pmcid": "PMC123",
                 "doi": "10.1234/example", "title": "Study A", "year": 2024, "container": "Journal A"}
        for key, conflicting in (("pmid", "999"), ("pmcid", "PMC999")):
            with self.subTest(key=key):
                second = {**first, "provider": "europe-pmc", key: conflicting}
                agreement = ScholarlyClient.reconcile_reference_metadata([first, second], first)
                self.assertEqual(agreement["agreementStrength"], "conflict")

    def test_identity_requires_distinct_providers_and_complete_shared_fields(self):
        base = {"provider": "crossref", "title": "Study A", "year": 2024, "container": "Journal A"}
        other = {**base, "provider": "europe-pmc"}
        reconcile = ScholarlyClient.reconcile_reference_metadata
        self.assertEqual(reconcile([base, base], base)["agreementStrength"], "weak")
        self.assertEqual(reconcile([{"provider": "crossref", "title": "Study A"}, {"provider": "pubmed", "year": 2024}], base)["agreementStrength"], "weak")
        self.assertEqual(reconcile([base, other], base)["agreementStrength"], "strong")
        self.assertEqual(reconcile([base, other], {**base, "title": "Different same-year study"})["agreementStrength"], "conflict")
        self.assertEqual(reconcile([base, other], {"raw": "Study A. Journal A. 2024."})["agreementStrength"], "strong")
        self.assertEqual(reconcile([base, other], {"raw": "Study A. J A. 2024."})["agreementStrength"], "weak")

    def test_doi_punctuation_is_preserved_during_identity_comparison(self):
        first = {"provider": "crossref", "doi": "10.1234/study-a", "title": "Study A", "year": 2024, "container": "Journal A"}
        second = {**first, "provider": "pubmed", "doi": "10.1234/study.a"}
        result = ScholarlyClient.reconcile_reference_metadata([first, second], first)
        self.assertEqual(result["agreementStrength"], "conflict")

    def test_title_decoding_retains_raw_provider_strings(self):
        raw = {"title": ["A &amp; B"], "container-title": ["<i>Food</i> &amp; Science"], "volume": "4", "page": "21-30"}
        result = normalize_crossref(raw)
        self.assertEqual(result["title"], "A & B")
        self.assertEqual(result["container"], "Food & Science")
        self.assertEqual(result["rawMetadata"], raw)
        self.assertEqual(result["pages"], "21-30")

    def test_provider_venue_aliases_and_issns_resolve_without_fuzzy_guesses(self):
        raw = {"DOI": "10.1234/example", "title": ["Study A"], "published": {"date-parts": [[2024]]},
               "container-title": ["Journal of Example Medicine"], "short-container-title": ["J. Example Med."],
               "ISSN": ["1234-567X"], "page": "123-127"}
        first = normalize_crossref(raw)
        second = normalize_europe_pmc({"doi": "10.1234/example", "title": "Study A", "pubYear": "2024",
                                      "journalTitle": "J Example Med", "pageInfo": "123-7",
                                      "journalInfo": {"journal": {"title": "Journal of Example Medicine", "issn": "1234-567X"}}})
        printed = {"raw": "Study A. J Example Med. 2024; 123-7.", "doi": "10.1234/example", "pages": "123-7"}
        self.assertEqual(ScholarlyClient.reconcile_reference_metadata([first, second], printed)["agreementStrength"], "strong")
        self.assertEqual(first["rawMetadata"], raw)
        self.assertEqual(first["issns"], ["1234567X"])
        self.assertIn("J. Example Med.", first["containerAliases"])
        second["container"] = "An unrelated journal"
        second["containerAliases"] = ["An unrelated journal"]
        second["issns"] = ["98765432"]
        self.assertEqual(ScholarlyClient.reconcile_reference_metadata([first, second], printed)["agreementStrength"], "conflict")

    def test_shared_issn_resolves_provider_venue_variants_but_never_year_conflicts(self):
        first = {"provider": "crossref", "title": "Study A", "year": 2024,
                 "container": "Journal of Example Medicine", "issns": ["1234567X"]}
        second = {**first, "provider": "europe-pmc", "container": "J Example Med"}
        printed = {"raw": "Study A. J Example Med. 2024."}
        self.assertEqual(ScholarlyClient.reconcile_reference_metadata([first, second], printed)["agreementStrength"], "strong")
        second["year"] = 2023
        result = ScholarlyClient.reconcile_reference_metadata([first, second], printed)
        self.assertEqual(result["agreementStrength"], "conflict")
        self.assertIn("year", [difference["field"] for difference in result["differences"]])

    def test_page_range_normalization_does_not_hide_different_pages(self):
        first = {"provider": "crossref", "title": "Study A", "year": 2024, "container": "Journal", "pages": "S123-S127"}
        for pages, expected in (("S123-7", "strong"), ("S123-8", "conflict"), ("123-127", "conflict")):
            with self.subTest(pages=pages):
                second = {**first, "provider": "pubmed", "pages": pages}
                self.assertEqual(ScholarlyClient.reconcile_reference_metadata([first, second], first)["agreementStrength"], expected)

    def test_pubmed_normalization_preserves_declared_journal_aliases(self):
        xml = pubmed_xml("123").replace(b"<Title>Example Journal</Title>", b"<Title>Example Journal</Title><ISOAbbreviation>Example J</ISOAbbreviation><ISSN>1234-567X</ISSN>")
        record = parse_pubmed_record(xml, "123")
        self.assertIn("Example J", record["containerAliases"])
        self.assertEqual(record["issns"], ["1234567X"])

    def test_discovered_identifier_agreement_requires_the_same_doi_everywhere(self):
        record = {"provider": "crossref", "doi": "10.1234/example", "title": "Sample", "year": 2024, "container": "Journal"}
        records = [record, {**record, "provider": "pubmed"}]
        reconcile = ScholarlyClient.reconcile_identifier_metadata
        self.assertEqual(reconcile(records, doi="10.1234/example")["agreementStrength"], "strong")
        records[1]["doi"] = None
        result = reconcile(records, doi="10.1234/example")
        self.assertEqual(result["agreementStrength"], "weak")
        self.assertIn("doi:identifier-unconfirmed", result["missingFields"])
        records[1]["doi"] = "10.1234/other"
        self.assertEqual(reconcile(records, doi="10.1234/example")["agreementStrength"], "conflict")

    def test_structured_pubmed_preserves_complete_abstract_and_directed_notice(self):
        result = parse_pubmed_record(pubmed_xml("123", [("ErratumIn", "456")], "First result. " + "More results. " * 500), "123")
        self.assertGreater(len(result["abstract"]), 4000)
        self.assertEqual(result["notices"][0]["pmid"], "456")
        self.assertEqual(result["notices"][0]["refType"], "ErratumIn")

    def test_known_positive_route_must_find_actual_notices(self):
        failed = ScholarlyClient(transport=SourceTransport(live_controls=False)).get_publication_status(pmid="123")
        self.assertEqual(failed["state"], "unknown")
        self.assertFalse(failed["surveillance"]["passed"])
        passed = ScholarlyClient(transport=SourceTransport()).get_publication_status(pmid="123")
        self.assertEqual(passed["state"], "current")
        self.assertTrue(passed["surveillance"]["passed"])
        corrected = ScholarlyClient(transport=SourceTransport(notice=True)).get_publication_status(pmid="123")
        self.assertEqual(corrected["state"], "corrected")


class PublicDestinationTests(unittest.TestCase):
    def test_private_credentials_ports_and_protocols_are_rejected(self):
        for url in ("http://example.org/file", "https://localhost/file", "https://127.0.0.1/file", "https://[::1]/file", "https://169.254.169.254/file", "https://user:secret@example.org/file", "https://example.org:8443/file"):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                validate_public_url(url)

    def test_dns_private_destination_is_rejected_before_connect(self):
        connection = _PublicHTTPSConnection("papers.example", timeout=1)
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        with patch("ocsl_review.fulltext.socket.getaddrinfo", return_value=answers), patch("ocsl_review.fulltext.socket.create_connection") as connect, self.assertRaises(ProviderError):
            connection.connect()
        connect.assert_not_called()

    def test_validated_address_is_pinned_and_tls_checks_the_original_hostname(self):
        connection = _PublicHTTPSConnection("papers.example", timeout=1)
        connection._context = Mock()
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        raw = Mock()
        with patch("ocsl_review.fulltext.socket.getaddrinfo", return_value=answers) as resolve, patch("ocsl_review.fulltext.socket.create_connection", return_value=raw) as connect:
            connection.connect()
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))
        connection._context.wrap_socket.assert_called_once_with(raw, server_hostname="papers.example")

    def test_slow_body_cannot_extend_absolute_timeout(self):
        response = Mock()
        response.read1.return_value = b"x"
        wrapper = _Response(response, Mock(), "https://papers.example/file", deadline=1)
        with patch("ocsl_review.fulltext.time.monotonic", side_effect=[0, 2]), self.assertRaisesRegex(ProviderError, "absolute timeout"):
            wrapper.read(10)

    def test_redirect_validation_and_document_header_isolation(self):
        first = Mock(status=302, headers={"Location": "https://cdn.example/source.pdf"})
        second = Mock(status=200, headers={})
        connections = [Mock(), Mock()]
        connections[0].getresponse.return_value = first
        connections[1].getresponse.return_value = second
        request = urllib.request.Request("https://papers.example/source.pdf", headers={"Authorization": "secret", "Cookie": "session", "User-Agent": "contact operator@example.org"})
        seen = []
        with patch("ocsl_review.fulltext._PublicHTTPSConnection", side_effect=connections):
            with open_public_https(request, timeout=5, allow_cross_origin=True, authorize_url=seen.append):
                pass
        self.assertEqual(seen, ["https://papers.example/source.pdf", "https://cdn.example/source.pdf"])
        for connection in connections:
            headers = connection.request.call_args.kwargs["headers"]
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Cookie", headers)
            self.assertNotIn("operator@example.org", str(headers))

    def test_metadata_cannot_redirect_credentials_to_other_origin(self):
        connection = Mock()
        connection.getresponse.return_value = Mock(status=302, headers={"Location": "https://different.example/steal"})
        with patch("ocsl_review.fulltext._PublicHTTPSConnection", return_value=connection) as construct, self.assertRaisesRegex(ProviderError, "changed provider"):
            open_public_https(urllib.request.Request("https://metadata.example/query?api_key=secret"), timeout=2)
        self.assertEqual(construct.call_count, 1)

    def test_public_download_redirect_to_private_host_fails_before_second_connection(self):
        connection = Mock()
        connection.getresponse.return_value = Mock(status=302, headers={"Location": "https://127.0.0.1/private"})
        with patch("ocsl_review.fulltext._PublicHTTPSConnection", return_value=connection) as construct, self.assertRaises(ProviderError):
            open_public_https(urllib.request.Request("https://papers.example/file"), timeout=2, allow_cross_origin=True)
        self.assertEqual(construct.call_count, 1)


if __name__ == "__main__":
    unittest.main()
