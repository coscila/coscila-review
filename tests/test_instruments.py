# SPDX-License-Identifier: Apache-2.0
"""Instrument registry: bundles, digests, pins, the unchanged v2 instrument and blindness of lab-facing files."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocsl_review import corpus_assessment, instruments
from ocsl_review.errors import ValidationError

ROOT = Path(__file__).resolve().parents[1]

# Study-identifying tokens (session, workspace and chat identifiers, file names, outcome figures and interval
# bounds, corpus totals, title fragments and a date seed) are kept here only as sha256 digests, grouped by
# length; lab-facing files, and every tracked file outside tests, must contain none of them at a word start.
# No-break and thin spaces count as spaces, so "12.5<NBSP>%" is the same token as "12.5 %".
FORBIDDEN = {
    4: {"0c90dd551bf12b5fecc7a9c5caa5a728d45b1c2a5f1760aca9331c12ce0df252",
        "c31e0fcedb154b3269a7fbf9b8e71a41d16af95fd4b16cb805df3dcdddf0e700"},
    5: {"0cbe66614a15a187a5aa341c71efafd84d550ff34dc178e773605f28f45cc84b",
        "1a5cf5c59619f15229f4f8c805d0667fd7c2e4001194717fb7ee62582f829045",
        "23c6a973a31b0bd5f99638209767556a02ac34bb793729382b6fd994d1e90b91",
        "486f4110f8bbcf7eda1c0878929785fddddfa708c01ab0a7ef55a79c8ca6d834",
        "787e3c87ea2a0aa33edd0cc62d12ffd6d5c5457f12bbeab3bc9cedeffc06c8e9",
        "844cb8bb059c33fa526cb7124d8c52c6ff309f5113241e4dd16e04f20c9b005d",
        "94957c17d26f4b6175d2162c79e1165bd4071fbba49ae5797c00b1b3131b31b7",
        "a46773d48e58ed47190eba1132d7d735ab2d06fd94859332b8ead7bc2e5163e9",
        "b8ad25220cb9a5bcb26f18dac02f87a1e947b58e5cba443d566601a4e9cd601e",
        "fc17580b56601023f5104f60e79b6e3390def8e30e35cc4fc0ab4ed1a5de3777"},
    6: {"2c1ea11a3e37bce82366e296bbb04a05204ae360b5046b7ebe103682bfbace78",
        "357bd9eef9d277dae8eabee6f41863f6a7489733bafdeb12c7770524a9e51b59",
        "36d383e4f228cc0bdbfcaaf4d78ef76bd7dc2767737a7af341e8c49277be30b7",
        "3e933e14fd7af6bce60513e218c94d1210e2e43eaecb68c911762ace10142a55",
        "47214e5b4a0d19d7eda978b74955ba4e914e0bb4020ecca9dc036241424917d5",
        "d7198881bf92a4872f10e9fa0ef00fb5c8860f56060f85642fcbe1d4ac05a6fd",
        "d88fb831cf45ec772adf0d3aa8537656fe3656b94c06965929d6cfe1d98c7e11"},
    7: {"6736ce4e60cd6dd9edd3bf565368a5cb70c18b0a5c53f4b032f6fc81badd7ff0",
        "9973b513d82595b8bd6b569b85ab24ea7d6793074697276db508ed424124e31a",
        "c0c83edd9625e9bb2d3cd03550dc250197624f743a1a5bd3dad969167bdc8347"},
    8: {"2991c4f2dbfc89e3e183e2afafd355e3901bce7b8c2752b280125ebd5648e769",
        "42474026dbf58ea83c4f5a17a2ce9f74e5690a9ab3b9195e8682d39ad3a17dd2",
        "4636f0a96191eb01b381ce832aa8c719e1a4a92d106067a4102b7534cd7bd48b",
        "7d24b99d06d2eaaacb3962cbd29ebce5c4ccd4c3be5a1ebae44c370ff1d60d5b",
        "7fcf1fa435db30d801a0cabcf6b52a5a01b13c63cd9cf2dd2c2ebe5ce99b20ef",
        "87e405c491a5436773e6386d96c2eabba587a224508a2eda3fcfe4333f3dfb27",
        "a18577d59248e450bed92c174eaa2b1fdeacd73d7b417a819085dd8543c7b3eb",
        "e89c2935a82eb5858fdb7b1ec108c72b5fc288730f5dfd123a53a591a625d4b9",
        "f08b56e242e5cab22bfce9b61d13b457a8f7bf49db4343ff2243a1f72aa76550"},
    9: {"5ae55e00a3e720f2689992cec849487cddbd3903ecabe29db9331004e2912045",
        "ac3fdda654a75afb24caa46cfc59612c03d3daaee4cbfdb94833df7eee51e24e"},
    12: {"2463f01a9b90a2ad08a97f6c854d48e18ced053a7f61906c01e12b4ab438990b"},
    42: {"d8e6d64915d843b393a00b1c75711cf9b95a45cb5ec3ca55f2194600e4daea33"},
    53: {"4a644f4c0b74832906e22a4322b92a083cc26e14fa38b131b03fc46b4c16684e"},
}
# Rounded, fraction and spacing variants of study figures, the forms a facts-lint example or a written range can take.
for _length, _digests in {
    4: {"3e239cbbd360793f0ebd640df0b475e267be0cf1d7d7c80a6250ba0fc6e1f566",
        "974c474e03d760995d1bac1d06fbcd28a8d62b16a5b53f212cbbe6134832939d",
        "a7071849fb72be7f6138ee4d048867bcd88f2250589bc6da81141fb8fce73d89",
        "aaae6b81b3239e64dbcd6dc0b5dfa117884d112b56ddaf59ab9ef9b8f94bb7d2",
        "f1e8aa5a065b37a6d729411d50b8531b35a836efa6ca8efb20d028d9df83d2aa"},
    5: {"f9cd6c42de299434f0cace60f6d4837206dddb9dddeee6a9e37869cf671859cf"},
    6: {"3bd54cc0263127e91067fca32cd9b3a1f0ec2f73e1c1647a88b93ad4033c2751",
        "928423789231f88fc7042d8140369886581e87d1d7c342385f687467fc5037fc"},
    7: {"52a3d7397e8776140a09443ec657d971371625d344c9208cfdee6e56661a144a"},
    9: {"52ae8db672be4012d76616a42c6b3093fa364d46c1b687c0e26895a566068f9f"},
    10: {"0040ce1bd1f1b649087327396c80c29d5b68f4a2d7861ecf52c0ed8ddcb97204",
         "039ed5ce153c651a4c083c0ce5a57b0831843bf7c13cc2ced001bee5d9dba80c"},
    11: {"b3d18e1164052ad49fcb59ee72190d6ca9594d6ccd2f04c2149633f4faf2fded"},
}.items():
    FORBIDDEN.setdefault(_length, set()).update(_digests)
# Further study figures and identifiers a final review found missing: population and readable counts with and without
# separators, corpus totals, the diagnostic bands in both dash spellings, token and runtime totals, the corrected
# report's title and file names, the snapshot's directory name, and the identifiers of further census sessions.
for _length, _digests in {
    4: {"0a95ca5e3efad73cd92ae12559d1189d05ff48b6344f0f83f760e59352f22689",
        "6c4c237fa6808f1c64a0a64254a296c0b006ede809a804447cd6dc06b46cb192",
        "aaae6b81b3239e64dbcd6dc0b5dfa117884d112b56ddaf59ab9ef9b8f94bb7d2"},
    5: {"139923821f09b19101c003fb15d7b873c0d37b528a80f61936eff528b1a0acbc",
        "20d4ea1b58b071504152736437b77fd13793af17b3a4ca0611f9793d0d36a838",
        "589b1174e8d1f3a929d4c9237c06614379e9d061503c19ede8c00529b320ec98",
        "69d325d196c62c699477760d45136fb4b56550cd8fc6ded59d5ed6f6e8bb1488",
        "ce7971f95aeac24e316891e78bfa924ceac221030e7f03ffdae5c0f9af06e4a5"},
    6: {"6dc2606829c647713389befda0fb36a684a41d042c738863a40bf4b060488ba7",
        "b3384b6dff9e1bef0865f9d58d8b78d43ae118f5fbe6ad874a3b032df584bfd8",
        "c603e8999513029e44d9e978c1fc67f6fe08f7dda2158d8a9d22ed7ebbaac7de"},
    7: {"28ad27422591424f71c86df882f94dfaf52a45930cf67e8486a27bd892fae388",
        "3007dd62825805d659236c8fbbbeddfbf6c0142ea348a48e41b93517856ffee3",
        "35a94fec15e6ecef565c0e271154a4490d7b3e6a2c524fa5105126af55be3d45",
        "52a3d7397e8776140a09443ec657d971371625d344c9208cfdee6e56661a144a",
        "b8c2168dd9b003c2831c38e953d3e93ca149a7c83ad6399af08f05b1637d6529",
        "f379cdca4ff319e198b2bb178c54cdeb6ce6aad81206092c55522c3f90c69fe2"},
    8: {"1003143bea389f7ef87fa8c910464010c8bc8c7d5864f116bb2a4878688f48c9",
        "322ade8662be4dd8776d5021ddb866ed3ffdd26d8d6c91b03ac5c2a0ad4bb668",
        "54b0dc5426c4e7100617f94b1421a71a42f8b986f395378e7b7f087e15b8fd44",
        "8d9174f77b32df3dd6bd11330a9f2f0e38582340619fb3ac472ba1bc3f4ceeca",
        "c08b71f7f21a6d9b060343d2426c243e742aea023f08074d0c46b54f6c89ca09",
        "dcc58a67a288e50ff9f31b805e7cf1a29e9ba2d188e8cce3a0ee944566d89d75"},
    9: {"0d5887fe37d9f3447ee1f57da00aee968a4e47adb40f1a129f679f10d45258a8"},
    10: {"b23d1ce0d23777d1e3d54a39edb274ec0e3ca91dcb8eda72a0e32344868f7da7",
        "d8c0e27bab7b0333437c043441f313ff792755325b8c5b37cfcb445142a5c3b7"},
    12: {"7e67e8069bd0cdf29a6f7608d7ebcc6a8cc181be232a119b7df0072c5cb6265d"},
    16: {"290b42a29e439a84354ca30fcfa035dca0d3eec9297f5ef35b77b89113b9a235",
        "9ceab4517efe93e787263de5fdcd5bbd5ea955ac47449f93453905505ad8138c"},
    20: {"b77790e3aec8b76e96fef34e34ca6667da8c87f786d5b01352c2e6b8cebdc7d0",
        "b965fe2d415f0f51958dcaeb121e5feb863b6258496ea78f078303213af0a9a0"},
    21: {"3837e95ba23a92e4ec96e2768e54ba4c6091f22c2fda291b219c511deafa5332",
        "715a0fcdccb251665c3abd899d877027259671e21c2bf9eb69cc9a9e11063ade"},
    23: {"31f89bc0c1520411347e937220e6c599ba8f5f67ebb56d9e6d4086d504015c81"},
    51: {"eae3d450ca367e62d119f56da83d611e6f5239d32ba2746dd08e8a72ecbcc531"},
}.items():
    FORBIDDEN.setdefault(int(_length), set()).update(_digests)
# Locale and spacing variants: the decimal-comma forms of every percentage, band, interval and ratio figure above
# (with and without the space before the percent sign), the no-space forms of the bands, and every separator form of
# the distinctive counts (none, comma, period, space and thin space, the bare forms of the counts that lacked them
# among them). Bare whole-number percentages such as "16 %" or "8 %" are left out on purpose: they occur in any
# literature and would make every scan fail. A three-digit count has no separator form and is too common bare. The
# scanners fold no-break and thin spaces into a space, so the thin-space digests matter only to a reader that hashes
# raw text.
for _length, _digests in {
    4: {"5b33003a928495b97792ac286d477b54dd20eb773c74ae2fb3653bc5950ad6dd",
        "79b130bc38e970edf25be2bb3cecd3227db25e0d9201346c71557d09ef6557cc",
        "7d1ccdfe7b15088f75affb300b4f370c68b3959384dafc65dfd2931b3ed2b58a",
        "e2aabeaac667528d2d6cccc7bf1e93fc2881914a2625aa218bace5a7634e38df"},
    5: {"1933753858a65e892538005879e535e767318ae22c18252e0c509d32b362b6a0",
        "1fd0e70370d3ce3d2b2e97a871f98ac708f0242ae0d31970274d281f5f1d3061",
        "3157acbe755094cbd3cd5073b189ec69942160c050bed0128077b36bab5b0684",
        "3b5db5a596152bc479df712e7aefc6a65fbe90c916f067b516c7e3bcf60487ff",
        "4521a5408f9a3f5897b540e1eae0071a36d84a412fbefb7dc9aaaf24ac24cd19",
        "56d04302692da5e01fbbf57e189f31b64439b130e3921333b97d27c88508c910",
        "63fd4c8aa416c95e8a0f87c22d26b5204c3e2c7e16e38e7ef4c4e3a5b6f5a784",
        "64867ccef48fb76438c68827873ceef8924ecc79a5752aed07204a48f1f18adc",
        "6d5887a6e360da476dc4fda8b5a023802485976fb77008d7fcdaa0471cfe9844",
        "748a9fd311cef8b015e1f9ed5d86277a35ffb72fac883349dcd12c7b02e69f0a",
        "749f1bf1d60d4e083bf3823f04d0446168a796ae01c4f1aa6f8fc6d188011fb3",
        "752b90c4000480a7224e10308a39f39a44552f9cc81809630950e28a711d75c6",
        "77cd9a9a89488f38ec43fef869ac73411b02f2e1d81117ac4974c4521da3c626",
        "85a77ee85b4bb4e1f45c53e66b90cc55a70c6b3f194ee9642ee20b1832338ce8",
        "8d4fbf8f632bc239812d177844db82469945304b3c8853eb45daa4236b20fc90",
        "a817d518ac48afbf9a17b579daab6ea3c3d41365324d96e0172be16e1a7d8375",
        "a9a57d9089b470ac89fe75db94f4be4e929ef84bed62ce10bf09bee77f151064",
        "b35db775173c9b651a7c42c419b421a0c20ba72e70f09a659401b0889d081b0d",
        "b422ce36c66e420a9d6d56d455712412821bd1d9df19fed57b98ee788cbe7dc9",
        "b43923ee71fb8e609f50be1cc34625181eb6cb0890e8a3dfa4f2801d630fe45a",
        "b8e3b46ba16356267374f6d7fa0185cb8d2e999d07f87934a8a95105ad0235c0",
        "b8f54e6ae200ba25ab07bbdab1c35f246b7bb886af79bd621608d35d1fa8567c",
        "d9a5fe2ea745a737bf4396ee5aeed4834cf019613bc210e1dc03bbe88de7be08",
        "debd9ec9a0a37e35d0ae3a99cacc610f897e50c8288188acdac613877082da25",
        "e290abc8edd2d13b2b3d73900682f45fe52621f8fd9317ba70d7d0e5a4426089"},
    6: {"1b62b330060e680a4dc1e99423d12b532a7a3332908522ef799fa6b5c51899ad",
        "3dc9a3c5704d3aff33aa952ec3f4d995a9360fd7ae449847a0a368b882d3eda1",
        "5374c68e5c83f2655f59b2b5efda644a8be682b6cbf84e3085152fae183ecf38",
        "7134c5de947b64ac10f25eb33b5425d512042a2e28f8bf68a074c940df8afe51",
        "7284a2f79b1405fbb2632f0cbf7d84a53173b34c8e44d626810890b150163622",
        "7c395f859b5168762b6a44d7f7830d76d17444371809160142d980b176ba0b85",
        "7fd8022dff43558fb58739f8f9594df52d6074414db4c6b59b54c64152c18b47",
        "919b65c1b7c320ccc85562475117449c0eb2e05fcc51ca3d00ae43fed0391e84",
        "aebbd5906f185b63fd3c9233f3f8301e539239c7df963c5982445d634235ac38",
        "b5aff7f81f3c6da0ffc7bf2d7b4b029a15be4c26a3f13abf7c6b1e81f8b29797",
        "b727a554491e2f4a797bcf4bac8c996cdc997a1b76ea5c41525a5c77edcf7a68",
        "db7eef30e81a52de5f7cb5f0db7b087973d45e70e75edf5c57e948e9fab75041",
        "fe530e18c7508d5b275ec2ec44d893e8b52e781652685d923c77d00169d97ec2"},
    7: {"2c89ba451a4e253ceeae47220e04f0841c8e37e0768d829b35976c6b148d7a67",
        "35ab4dadf3b65b9b93553e96140b165ec5e0777197e3f4eb399e98e69cd8df74",
        "6204401fcadafcc065d80caf46ebf17470654c7537590750a91762dd734591b9",
        "7a97f29939ff0dd7fdf755be60d85001978b2a4fe1738057fcd58161d17f6a23",
        "805b8bda9b3c5f2ad50a24eca3f05083d32a54e57e3eb982817de126c7a9fce6",
        "881fd4c3c3da5dddf55211691759de74a8aa1bc9bcf0eecf57d2cfc728ca18d7",
        "9978bcbbde3ed1b96939a94a676dfb7ad5a9771431d7510fd0d6ab7ca5b2d32a",
        "adae7fb2271aa60afa9d44363d5be1be7ed1b872af33e735a8afb8396cd2e254",
        "b3fc9ce779d95b0024ca57dafdfc14c97fb712f36042ea5908f05532c4740d11",
        "c47d610b0d6422de1dabf9b5d38e7300c60e48309e88ae5ef34603a5367cb034",
        "cdff45137d15259f9e393179e9e8b8de9610f06b2f2211accee3b56dfb86df3a"},
    8: {"2c1da5d4c44fb7dcd50d23b7486e374f6475579210516f5d96f30c6b04bdc44f",
        "fd57aa550a02daed53c7490c47f7a0353fd33ab9fa490e148af8be973cb72b33"},
    9: {"4c847dd1bcd9909e59b67000147b3676297fce7283492bc027af2bdad526d31f",
        "65b7b28c92453dbfbacec50c7d799f5bf5e436da99b99ed5e01b08791f9c4435",
        "8080a4067400c1c8d31d96b9c9f0ff9826e3df2fada69d8ea3e6fe78f5d59f04",
        "97df374a8095375f2a929590cd07ed0bf472ef00c9ef17737f3414fe8599e128",
        "b232599af1b5d756470f55748af2ded47822e3d694032e3c75648d2bf925296c",
        "be09835489965d4d74ff8d4ddf6c5091de79654ce7ccfec2311b509c08ff4fe5",
        "cbfd95bd5a0d21b67951f32926246ec85d8220992d9a5308325342f6e35dc4e8",
        "ea957e805be52702f09d42a53e66cb8bbd9bf7afba9d4e570d0560767031819c",
        "f4f34e8d41dc0eb1bdafb368da76b3b7d8fa1952effd41bbeb86e76f80cbfcb4"},
    10: {"53296370dc9ec0a96c0fd4a212a865f65ea1a4b969b5953caad37f2be3295a35",
         "7a3014813c09b431831ef4a51b1273cc52510b9976311054aafe1806810a8ebd",
         "91caada049bd96dd86d0aa91510e2e6f544ee2375a6f72c0fb7497ef7ef355f8"},
    11: {"3af9d65593904463222ef4194529ed2551e4c6aac04b26655212df7badf79c26"},
}.items():
    FORBIDDEN.setdefault(_length, set()).update(_digests)
# The same digest list is pinned in coscila (scripts/ops/leakage-tokens.ts, from packages/lab) and coscila-writing
# (tests/test_blindness.py): sha256 of the sorted digests joined by newlines, and their number. A change to one table
# changes this fingerprint, so all three copies change together.
TABLE_FINGERPRINT = "af696c1f0ed8242f5271fc5f6867e20b64a833d15121a881b7d2443962d6b51a"
TABLE_SIZE = 150


def table_fingerprint(table) -> tuple[str, int]:
    digests = sorted({digest for group in table.values() for digest in group})
    return hashlib.sha256("\n".join(digests).encode("ascii")).hexdigest(), len(digests)


# Everything the lab sees: skills (with their assets and references), instruments, prompts, docs and the
# modules that build prompts or runtime output.
LAB_FACING = ["skills", "instruments", "prompts", "docs", "README.md", "schemas/instrument.schema.json",
              "src/ocsl_review/instruments.py", "src/ocsl_review/citation_census.py",
              "src/ocsl_review/census_extraction.py", "src/ocsl_review/census_runner.py",
              "src/ocsl_review/census_broker.py",
              "src/ocsl_review/scores_db.py", "src/ocsl_review/estimands.py", "src/ocsl_review/corpus_facts.py",
              "src/ocsl_review/study_cli.py", "src/ocsl_review/census_abstracts.py"]


SPACES = str.maketrans({"\u00a0": " ", "\u2007": " ", "\u2009": " ", "\u200a": " ", "\u202f": " "})
# Reviewed exclusions from the scan of every tracked file: the tests hold the digests themselves and synthetic
# fixtures, never the tokens.
SCAN_EXCLUDED = ("tests/",)


def tracked_files() -> list[Path]:
    """Every file of the checkout outside the reviewed exclusions (git's index, else a walk of the tree)."""
    import subprocess
    try:
        names = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True,
                               check=True, timeout=30).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                 if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    return [ROOT / name for name in sorted(names) if not name.startswith(SCAN_EXCLUDED) and (ROOT / name).is_file()]


def forbidden_hits(text: str, table=None) -> list[int]:
    text = text.translate(SPACES)
    hits = []
    for start in range(len(text)):
        if start and (text[start - 1].isalnum() or text[start - 1] == "."):
            continue
        for length, digests in (table or FORBIDDEN).items():
            window = text[start:start + length]
            if len(window) == length and hashlib.sha256(window.encode("utf-8")).hexdigest() in digests:
                hits.append(start)
    return hits


class RegistryTests(unittest.TestCase):
    def test_registered_instruments_verify(self):
        self.assertEqual(["citation-census@1", "citation-support@2", "claim-check@1"], instruments.available())
        for instrument_id in instruments.available():
            report = instruments.verify(instruments.load(instrument_id, check=False))
            self.assertTrue(report["ok"], report["problems"])
            self.assertRegex(report["digest"], r"^[0-9a-f]{64}$")

    def test_digest_is_stable_and_covers_files_and_modules(self):
        census = instruments.load("citation-census@1")
        self.assertEqual(census.digest, instruments.load("citation-census@1").digest)
        description = census.describe()
        self.assertEqual({"systemPrompt", "fullTextNote", "labelSchema"}, set(description["files"]))
        self.assertEqual({"validator", "builder"}, set(description["modules"]))
        self.assertEqual(census.manifest["validator"]["sha256"], description["modules"]["validator"])
        self.assertNotEqual(census.digest, instruments.load("citation-support@2").digest)

    def test_bundle_fields(self):
        census = instruments.load("citation-census@1")
        abstract, full = census.tier("abstract"), census.tier("full-text")
        self.assertEqual((40, 0.0, "none", 1), (abstract["geometry"]["itemsPerCall"], abstract["request"]["temperature"],
                                               abstract["request"]["reasoningEffort"], abstract["repair"]["passes"]))
        self.assertEqual(2200, abstract["evidencePolicy"]["abstract"]["cutCharacters"])
        self.assertEqual(3, abstract["evidencePolicy"]["citingSentences"]["max"])
        self.assertEqual((5, "low", 11000), (full["geometry"]["itemsPerCall"], full["request"]["reasoningEffort"],
                                            full["evidencePolicy"]["window"]["characters"]))
        self.assertEqual({"floor": 3000, "perItem": 700}, {k: abstract["request"]["maxOutputTokens"][k] for k in ("floor", "perItem")})
        self.assertEqual("nvidia/Qwen3.8-Flash-Next-NVFP4", census.manifest["requiredModel"]["served"])
        self.assertEqual(20000, census.manifest["sensitivities"]["no-truncation"]["sampleEntries"])
        self.assertEqual(1, census.text("systemPrompt").count("{{journal}}"))
        with self.assertRaises(ValidationError):
            census.tier("panel")

    def test_v2_is_registered_unchanged(self):
        v2 = instruments.load("citation-support@2")
        self.assertEqual(corpus_assessment.PROMPT_PATH.resolve(), v2.file_path("systemPrompt"))
        self.assertEqual(corpus_assessment.SCHEMA_PATH.resolve(), v2.file_path("outputSchema"))
        self.assertEqual(hashlib.sha256(corpus_assessment.PROMPT_PATH.read_bytes()).hexdigest(),
                         v2.manifest["files"]["systemPrompt"]["sha256"])
        self.assertEqual(corpus_assessment.output_schema(), json.loads(v2.text("outputSchema")))
        self.assertEqual("citation-support-v2", corpus_assessment.PROTOCOL_VERSION)
        self.assertEqual(corpus_assessment.MAX_BATCH_ITEMS, v2.tier("abstract")["geometry"]["itemsPerCall"]["max"])
        self.assertEqual("ocsl_review.corpus_assessment", v2.manifest["validator"]["module"])

    def test_ids_and_unknown_instruments(self):
        self.assertEqual(("citation-census", 1), instruments.parse_id("citation-census@1"))
        for bad in ("citation-census", "Census@1", "census@0", "census@x"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                instruments.parse_id(bad)
        with self.assertRaises(ValidationError):
            instruments.load("citation-census@9")
        with self.assertRaises(ValidationError):
            instruments.module_path("os.path")


class TamperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instruments"
        shutil.copytree(ROOT / "instruments", self.root)
        patcher = mock.patch.object(instruments, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_changed_prompt_fails_verification(self):
        path = self.root / "citation-census/1/system-prompt.txt"
        path.write_bytes(path.read_bytes() + b" ")
        report = instruments.verify(instruments.load("citation-census@1", check=False))
        self.assertFalse(report["ok"])
        self.assertTrue(any("systemPrompt" in p for p in report["problems"]))
        with self.assertRaises(ValidationError):
            instruments.load("citation-census@1")

    def test_changed_settings_change_the_digest(self):
        before = instruments.load("citation-census@1").digest
        path = self.root / "citation-census/1/instrument.json"
        manifest = json.loads(path.read_text())
        changed = copy.deepcopy(manifest)
        changed["tiers"]["abstract"]["geometry"]["itemsPerCall"] = 20
        path.write_text(json.dumps(changed))
        self.assertNotEqual(before, instruments.load("citation-census@1").digest)

    def test_module_pin_is_enforced(self):
        path = self.root / "citation-census/1/instrument.json"
        manifest = json.loads(path.read_text())
        manifest["validator"]["sha256"] = "0" * 64
        path.write_text(json.dumps(manifest))
        report = instruments.verify(instruments.load("citation-census@1", check=False))
        self.assertTrue(any("validator module" in p for p in report["problems"]))

    def test_schema_and_token_count_are_checked(self):
        path = self.root / "citation-census/1/instrument.json"
        manifest = json.loads(path.read_text())
        manifest["unexpected"] = True
        path.write_text(json.dumps(manifest))
        report = instruments.verify(instruments.load("citation-census@1", check=False))
        self.assertTrue(any(p.startswith("schema") for p in report["problems"]))

    def test_paths_cannot_escape(self):
        path = self.root / "citation-census/1/instrument.json"
        manifest = json.loads(path.read_text())
        manifest["files"]["systemPrompt"]["path"] = "../../../pyproject.toml"
        path.write_text(json.dumps(manifest))
        report = instruments.verify(instruments.load("citation-census@1", check=False))
        self.assertFalse(report["ok"])


class StudyBundleTests(unittest.TestCase):
    def test_study_bundle_digest(self):
        census = instruments.load("citation-census@1").digest
        args = dict(protocol={"seed": 7, "window": ["2001-01-01", "2002-12-31"]}, study_prompt="methods live in skills",
                    instruments={"citation-census@1": census}, dataset_versions={"evidence": "a" * 64},
                    package_tags={"ocsl-review": "0.1.0"})
        first = instruments.study_bundle_digest(**args)
        self.assertEqual(first, instruments.study_bundle_digest(**args))
        changed = instruments.study_bundle_digest(**{**args, "protocol": {"seed": 8, "window": ["2001-01-01", "2002-12-31"]}})
        self.assertNotEqual(first["sha256"], changed["sha256"])
        with self.assertRaises(ValidationError):
            instruments.study_bundle_digest(**{**args, "instruments": {"citation-census@1": "short"}})


class BlindnessTests(unittest.TestCase):
    def test_lab_facing_files_carry_no_study_identifiers(self):
        files = []
        for entry in LAB_FACING:
            path = ROOT / entry
            files.extend(sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path])
        self.assertTrue(files)
        for path in files:
            if not path.exists() or "__pycache__" in path.parts:
                continue
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual([], forbidden_hits(path.read_text(encoding="utf-8")))

    def test_every_tracked_file_outside_tests_carries_no_study_identifiers(self):
        files = tracked_files()
        self.assertGreater(len(files), 50)
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual([], forbidden_hits(text))

    def test_bundled_study_prompt_offers_no_path_to_earlier_results(self):
        prompt = (ROOT / "skills/ocsl-review-reference-analysis/assets/meta-meta-literature-review-prompt.md").read_text()
        for phrase in ("import-legacy", "import-snapshot", "existing approval", "run state", "historical scores",
                       "earlier judgments", "original census", "40-item"):
            self.assertNotIn(phrase, prompt)
        lessons = (ROOT / "skills/ocsl-review-reference-analysis/references/census-workflow-lessons.md").read_text()
        for phrase in ("2,200", "11,000", "Forty-item", "first three citing sentences", "earlier scores"):
            self.assertNotIn(phrase, lessons)
        for skill in ("ocsl-review-reference-analysis", "ocsl-review-citation-census"):
            text = (ROOT / "skills" / skill / "SKILL.md").read_text()
            self.assertIn("citation-census@1", text)
            self.assertIn("ocsl-review study", text)

    def test_files_the_census_skills_route_to_name_no_earlier_study(self):
        """Phrases that tell a reader an earlier census, its manifest or its selections exist; whitespace (a line
        break inside a phrase) is folded before matching."""
        analysis = ROOT / "skills/ocsl-review-reference-analysis"
        routed = [analysis / "SKILL.md", ROOT / "skills/ocsl-review-citation-census/SKILL.md",
                  *sorted((analysis / "references").glob("*.md")), ROOT / "docs/census-instrument.md",
                  ROOT / "docs/corpus-review.md", ROOT / "docs/citation-census.md", ROOT / "docs/capacity-probe.md",
                  ROOT / "docs/storage.md"]
        phrases = ("earlier census", "earlier study", "earlier studies", "earlier runs", "earlier run's",
                   "earlier population", "earlier manifest", "earlier acquisition", "earlier corpus",
                   "legacy-original-source", "oncology audit", "private record set", "are comparators",
                   "historical oncology", "earlier judgments", "original census")
        for path in routed:
            text = " ".join(path.read_text(encoding="utf-8").lower().split())
            with self.subTest(path=str(path.relative_to(ROOT))):
                for phrase in phrases:
                    self.assertNotIn(phrase, text)

    def test_token_table_is_the_digest_list_of_the_lab_and_writing_copies(self):
        self.assertEqual((TABLE_FINGERPRINT, TABLE_SIZE), table_fingerprint(FORBIDDEN))
        for length, digests in FORBIDDEN.items():
            for digest in digests:
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertGreaterEqual(length, 4)

    def test_scanner_detects_tokens_at_word_starts_only(self):
        table = {5: {hashlib.sha256(b"7.7 %").hexdigest()}}
        self.assertTrue(forbidden_hits("a share of 7.7 % here", table))
        self.assertTrue(forbidden_hits("a share of 7.7\u202f% here", table))
        self.assertTrue(forbidden_hits("a share of 7.7\u00a0% here", table))
        self.assertFalse(forbidden_hits("a share of 17.7 % here", table))
        self.assertFalse(forbidden_hits("a share of 1.7.7 % here", table))


if __name__ == "__main__":
    unittest.main()
