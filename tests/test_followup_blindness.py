# SPDX-License-Identifier: Apache-2.0
"""Blindness of the follow-up audit's files: no study identifier, no reference to earlier work, no carried figures.

The follow-up describes a generic audit of claims and citations in a corpus of review articles. Its strata sizes and
rates come from the run's own data, so none of its files may carry a figure:

- every follow-up file, its tests and its fixtures included, passes the hashed study-identifier scan of the census files
  (``test_instruments.FORBIDDEN``) and the follow-up's own digest tables, after invisible characters are removed, the
  fullwidth percent sign is read as ``%`` and no-break and thin spaces are read as spaces;
- ``FIGURE_DIGESTS`` holds, as sha256 digests grouped by length, the reference-derived figures, identifiers and named
  findings the follow-up's design was sized with, in their spellings (bare, comma- and space-grouped, decimal point and
  comma, with and without a percent sign or the words percent and per cent, k and M forms, ranges with either dash,
  ratios); no tracked file outside the census's own tests may carry one. ``FOLLOWUP_FIGURE_DIGESTS`` holds the short
  forms too generic for every file (small whole percentages, small ranges, short k forms), scanned in follow-up files.
  Bare integers below a thousand, years and round numbers are not hashed: they cannot be told from ordinary numbers,
  and the figure scan below catches any percentage. The figures themselves are never written in the repo; the digests
  were computed outside it;
- the lab-facing text (skill, instrument prompts and schemas, docs, the protocol schema) and the follow-up's source
  modules hold no percentage (with a sign or in words) and no grouped count at all;
- no follow-up file names earlier work: the phrase scan folds case, whitespace, hyphens and invisible characters.

The two blindness test modules hold the digests and the phrases themselves and are left out of the scans of their
own tables, as ``test_instruments`` leaves out the census's tests.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from test_instruments import FORBIDDEN, forbidden_hits, tracked_files

ROOT = Path(__file__).resolve().parents[1]
FOLLOWUP_SOURCES = ("src/ocsl_review/followup_*.py", "src/ocsl_review/citation_function.py",
                    "src/ocsl_review/numeric_check.py", "src/ocsl_review/claim_match.py")
LAB_TEXT_GLOBS = ("instruments/citation-function/**/*", "instruments/numeric-check/**/*", "instruments/claim-match/**/*",
                  "skills/ocsl-review-followup-audit/**/*", "docs/followup*.md", "schemas/followup-*.json")
FOLLOWUP_TESTS = ("tests/followup_fixtures.py", "tests/fixtures/followup/**/*", "tests/test_followup_*.py")
FOLLOWUP_GLOBS = FOLLOWUP_SOURCES + LAB_TEXT_GLOBS + FOLLOWUP_TESTS
# The modules that hold the digest tables and the phrase list themselves.
SCAN_TABLES = ("tests/test_followup_blindness.py", "tests/test_instruments.py")
PHRASES = tuple(" ".join(phrase.replace("-", " ").split()) for phrase in (
    "earlier census", "earlier study", "earlier studies", "earlier run", "earlier runs", "earlier run's",
    "earlier population", "earlier manifest", "earlier acquisition", "earlier corpus", "earlier audit",
    "earlier judgments", "earlier scores", "earlier results", "previous census", "prior census",
    "reference census", "reference run", "reference scores", "reference study", "original census",
    "oncology audit", "historical oncology", "private record set", "are comparators", "legacy-original-source",
    "reproduction chat", "september panel", "known retracted",
    *(f"{when} {what}" for when in ("previous", "prior", "past")
      for what in ("run", "runs", "run's", "study", "studies", "study's", "census", "censuses", "audit", "audits",
                   "results"))))
INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))
DASHES = str.maketrans({"-": " ", "\u2010": " ", "\u2011": " ", "\u2012": " ", "\u2013": " ", "\u2014": " ",
                        "\u2212": " "})
SPACES = str.maketrans({"\u00a0": " ", "\u2007": " ", "\u2009": " ", "\u200a": " ", "\u202f": " "})
FIGURES = re.compile(r"\d+(?:[.,]\d+)?\s?(?:%|percent\b|per cent\b)|\b\d{1,3}(?:,\d{3})+\b|\b\d{1,3}(?: \d{3})+\b",
                     re.IGNORECASE)
FIGURE_DIGESTS = {
    4: {
        "028e07cc23a20158b5b19ff796b63d5575b74678a818211b02f319567318020d",
        "084ae23e6996e701addeeb7dcf33696f32dcb4ab78c0ca2026f8f5dbbe5a81d0",
        "0a3d3f4a3a714afc3d0de4d2ad8a1ae44093e56d3ecfbcc2041f9899ea49a33d",
        "0a95ca5e3efad73cd92ae12559d1189d05ff48b6344f0f83f760e59352f22689",
        "0fb33ae72b5ce75bdca5cc19cacfc170fb1a2ad9df7f69cc6b8eafa9daa80ec8",
        "1392bb0a092127025d1df338eb05ea998173c7b06262430dc6c0f8fc4d4fd2a7",
        "2f0cb305a56a6599284b9db62db4caf32babbcd48f89a5a4da7dad5b59958437",
        "38448b44698eea450f2058ed7081ed34fcf8e7348a22fa0723304806ccba1626",
        "3ba982075eba89702c1637b9ce03d8a3d8b6dce9434dd07a4c30b5525ab27850",
        "3cb81c72e3c44a9c6a2fda94952ab34e0b0a89b0e7cfb6330ed371a0eda636bc",
        "42d9972e972e40956658f1b4f3bd676512837741de686d03f447d8015feb6a6c",
        "43bfac185bca9cb816499fa8c3da1e3f78f937c01c4eea1d82e14cef3f550754",
        "46795f92da7f1acffe85004a0b40508283f72489f2f32cb05c4309fc346a9196",
        "701e0e925f7fb4139e62f976a2703ad8c67c20f906559026b36c8bcd4311e532",
        "77a9775e9144600a59387c77e04c4ae05e67adf0e03e5a583ec69041dc8cd7eb",
        "8487c11dabd0a6cb18636649e9e5df1d7ab820aabd4b49ae0f327e571d68e43f",
        "858a794b9a1df6e2fa1e6258cafa1f3df7f31ff877c887107e245163fa52fbdc",
        "8cf04f0d07191f042b1d11880ab80618c2680e8e03bbacc60f9e31160d4fa87f",
        "9b8a3b7ea1f24fab9db73fa63b9c60b5e702f6d79c04d7cc6c51567c74820dcb",
        "a50dd9023233ad57f6bc22e7a1aff046943ace27703282f442b24403b9a3e94b",
        "a51fdbebba5eab3edfd71da08e669e381d0a1dd39a13f7871470b4411117fa05",
        "b41f1aff4d998f2ee49db0454b48ed69cb5c0e499e9bd1608b5d2c0b4f6cccfe",
        "be1722b1d104ffb2753260682954754d9757b986013823c5df20f28e0f74e655",
        "c5080dbc42e822a31c72ebf171d79394f57bba1e0511bad4a721c2c0f62ae6b3",
        "d9dba9d2a620acf4e2eb85570d63fb57a90805b59295bf1b971ffa61dee87b07",
        "f4a8b211507de854cc65654f39c173c63edac69501ad158c8595b6260fe670ce",
    },
    5: {
        "0901dbfa2211d4d109464e0df26e87e99b6224ab33df3acf622195917cd5fa0a",
        "093c3e9af257426a1fbe48c2040f827a4f1a2f1e3d403ec3890c86303e82d233",
        "0a5b523762e515dea07795de4816deaf98daa625957b2950845970ebd9512915",
        "0cbe66614a15a187a5aa341c71efafd84d550ff34dc178e773605f28f45cc84b",
        "0e81650ddaea5a2ed7262541944cccdec38e6ccdfd5fff481a40b457aee75055",
        "16451a7d9f9b3a6de31d7b4971d9a5dcef2e163fce3789fb30c073f90ee27626",
        "1bf17bd3e7756e0578046d2ce28445df00a29bbefd54c9f82ae76eabd31b8a17",
        "1d64e2d60a486d3d9a39ff8bcba384ae062c0aac79d159c733a3550bfb31a660",
        "21e4cc686d0a7a5f65b9fcad72d35584c886969c5901b7717491729ed995d125",
        "2277b5ec2fec4267499cccf03f93db883986630c6bb32039de0dc615dc92056f",
        "227de85211f8ff28078af69ce4b53e0b47fbc3240f9b289abbd2c0e666e178b3",
        "22f3acdfe502190e339ad08ed8d3857f24a28ade0639cab9fe5b2c779ac6b926",
        "27796efa28436ad3ee8f3f0893f738e28103355888c54ba510d6d2b5cad54a27",
        "28f8f980daa9e6beade84549d93d6f553c743689f40f0150f85b20e63403d294",
        "2b4e34ed0413d9f425d4026ebd5ca67b9238fe4d412a47a0a2919d67f8c7581e",
        "2e2ddc320844ba5b20a3d9ee6ccc7b2e218394811ac44bad96910d3f62b2f952",
        "32953d6587540c2fc688a85fdf85729579d5d40d5c70bab18fcd62337b085243",
        "339dce68ac0c30ebde511b5b33cd970022796325b4d97cba671c1c23a8656807",
        "3cb88a6f2dc249e9387d6cc977f08fcf641893896b6ce1a430417a63d0d2f3f8",
        "3cc27e4bdccc0437a27608bcc61aa8bcdbffcd39786342ea553a0d006cf9f38c",
        "3f9d3d7e32da1c15f0027adf2b2a0884a8e9f5c82d8882b731fd2b08c314664d",
        "4026af0778515eef38fd74fe3d9d80ff334264a0e67f11c722c7924edad8437d",
        "4244438e2ac77ce6c8f3e8e9e5213ed618c453899d5adf2100fba435b8cc1765",
        "4bccdade7037176c24ce5d88f8aeca5c82f71860c18c0db5a3bd9981546c16ad",
        "4e0eb64c19431a0c596ca60fbc055477964c9a759f6e24d2a6eb37a4fd8bddac",
        "504abe59fbbd9fca0182c17c63cb6e713aebf688ea5148444f096c3ed240e45a",
        "50ecf627fb988f9fac0108aa072a523917c1c3c2bc80478d3d9a38f1daea9b3c",
        "52be6c4a54592a5e4f23f68a8dc10b086565cf40b003b8a64c8580dbfc811502",
        "53645c2bca57e8d07b2f1d5859c853d338a13ad4be54caa011eddb0ce0f44788",
        "589b1174e8d1f3a929d4c9237c06614379e9d061503c19ede8c00529b320ec98",
        "60a400ecee3962ee2f16f2c400a8800aadf9261509bceebe34acfd944187e517",
        "655bde51e49b0e2c57b177976661d8a5c8db9ade4813e1abc0896cb384433343",
        "6e1fc34ae243a58e2432456e2e6466f4f7a86e651b1b536d262b88161133d7f8",
        "748a9fd311cef8b015e1f9ed5d86277a35ffb72fac883349dcd12c7b02e69f0a",
        "757e7356ab22e0eae4e7f74224d3205699ff5a9731b60900a86f6bb689c16858",
        "766ee8d5cd28ff5cbdf204c7df18ec6b778763ce9abfb188ba6f1b5f260fb647",
        "7c75488c5a18a6e512b39874823d62d2afec1ef6b08c2bf3239cb499c9e1835e",
        "7de54e2da10a8a944b188c4ade3586a5ae3fb2f8a5ac74602f37db3f809b2228",
        "80618d6e3429ed06a2c85657dd0644c4e9bba5fe88d7797b76ba5351806632c5",
        "94d936d875eb11f93c6d611a1fbe728fc5578b780c91f63000a1d4a9cfadc234",
        "9cfda89402e341a3f2f757e0083c953d92242cd7fbd0b49ec345c2837bf1f4fb",
        "9e6f132fb0a5f9fee9d74bf711419e4723a11405474f3aa7aa0954e6054e4395",
        "a29eb31e8e566f893e33558ab1bfa503e447d53a0797e4a903e2a922f960ea38",
        "a49e0bcd4c07161cabb88a642869752763dd864a868136829787085eb70017bf",
        "a75212acbf60de5b5f8758dbbee70c4e6eccd23640f70ee02cb93bb0bf5064c8",
        "aad3bd68e296aa9340b642eae200b731bf5f1f8a8292d3a15814fa91b889c798",
        "b16c9ca63efac5a17326d87906c12d6b862a58409db7a4bde01270b18a7b0083",
        "b3840257a5dac87ce62934a8b70f3c02e00ecb0a08a01bc1399173c337d7b90f",
        "b7fa9d2aa1dc48a2b622da53ac927dd3c947446831cbb6959d9cf3620e1c15d5",
        "b8e3b46ba16356267374f6d7fa0185cb8d2e999d07f87934a8a95105ad0235c0",
        "be852f93497009a42f47da1b1304a318fba7c41f22ae1b86938571965dc13f86",
        "c8aa2c0b8d156dfe9a4cdfa27444769354853967d69221b04b8f6badb55893bf",
        "c950a26be465cbbb1d761a21212a37af0e4e8714fd8bea4c31ea205fb7716702",
        "ca4067397792254a8ca8818801681679eb5d2c85f0e2c106bd4f6b547303934f",
        "ce7971f95aeac24e316891e78bfa924ceac221030e7f03ffdae5c0f9af06e4a5",
        "d4ce155606bbbd144c69572fb392ba9cb627dc4258e86fcdb10e5b970e9b2641",
        "d6570b4220f710f977f0ea1f63450dff4167c39a4e9f6322999efce34c3e283e",
        "db15dc0dfce5034fb0741944cbc21b14a4b07e1184481a60147d35a3542394ae",
        "dda370958771293721e90f23d454562d7420525e167f880d9f4428e78cb56e7e",
        "dfa2ddc46cdde5179fe282340b9486c64e8b43ca24a7c49b613a1b5be1a6d24b",
        "e283f864c3b720b60d1159b0453a3055987cd96765f85bf007c33583fdb7a350",
        "e290abc8edd2d13b2b3d73900682f45fe52621f8fd9317ba70d7d0e5a4426089",
        "e50de96a9c363587c021b4a65a8f1846b2f3484819649bdd244f496d9033dc14",
        "ebd3da382fd0ba60ba832755c5d2e000065f15078a9a507422aebf53b1dc6d24",
        "ebf9917b865ea0cae4338d1a8309e681ce0b69ef441cc0c1a822f1b67350ce5f",
        "f65429a0aec7a0d90ff25103b75e8eb2a3706656e41a69f3367ce105cef5de85",
        "ff340e44b2e5a3ff6fb19951f537bf4b645b18348c9627dc3d45b59d883c4282",
    },
    6: {
        "06d0c1a6923d7bf5ea50a98f5bb407ee4c0c984a0ce92a52fde8c1e75cbf465e",
        "0d4e26ba65649b0f16554af295960fe4b95641890eb70c92b70d31ee9cb9e1fb",
        "10940632a1ac692358bd2d4d683636820fc04ecb597b77f626939adf8f9681bc",
        "1309c0d56b86bc901c237a56de8ddb081d75f34782876a71cfd6154a44b782fc",
        "22c49ecfa26ecb74fd3dbd1d778b0354160c7aba63e858f4adcfe7fd1eddd9c8",
        "2c1ea11a3e37bce82366e296bbb04a05204ae360b5046b7ebe103682bfbace78",
        "32ea479f9361c955f8b8e08c8edc8f9ab0949bd8f295c0770f3fb9a55bb2a48e",
        "342149c97f13e20f9861ff1bef3e1f25357d30b9c7640f7ef1bbb59d58d55708",
        "38ce57c689ab838b6c1bfb869725a3af92cb24a776022c152e7e68d0ac1654de",
        "3dc9a3c5704d3aff33aa952ec3f4d995a9360fd7ae449847a0a368b882d3eda1",
        "41bf5dee46dd5fb29b48a3eef36e1245480e0fa1ee17a4ac4a1d2e8297859076",
        "44c5d16250b3deb93e848be61003daeeaff7d9a7177dadc389b68d2c22fc8bd8",
        "49aa20d740d12923ee1f5c127bb8aecebd1b85eb8327f14173322ac652421114",
        "56eabd544df10cd9797c82cb153e0f9da604b984ec0378a75fc7d990e46a4350",
        "57415c89b832f1a289c32f7ba37096c938164016dea490855e465e4e925c7772",
        "5e23f1ba3dbeafd8a6066c909c416857b5c0ea66d5628d10facacd2929224a25",
        "604a303d6bf3131181a161ab2adda108d7837d1b848440ed438b9a4d74306e21",
        "66ce716f1a453c2a320e4025b29538f9bbf0f263d1496384a9beb9fa28f2d8f1",
        "6b259be1f1d61defcf3f8be4b7166974bac01b79f42ae3440cb5d5b50eab1585",
        "71b09f11ce9c9795d8a6a3926d942c0983b04c604f60c898b7fe611e19e84798",
        "74fdc9f2cf9631c5d9142b06f00c646ff3730bdb74ef1f16cf0d23b81d9838c6",
        "75e8e33a74898d1eeade7c7dbf663080e453bd2aff38032926bcb9932d43f057",
        "781f2bef0ef9e1253719ac5fc9b9c61ef989b37171a2c83401a6a6912c54d683",
        "79b3cb3302ca64f40df467915865fe0e05d02e0b231a57417e407fa0fe3ec8de",
        "8a3b519e5405f2a2c3f2b08983d75f2eefcbbce5fb0f3683724abce365cbdaff",
        "a3183406b267eac701c3625f4eebe2af0a6a2305a19e32fd2821fa1088d1e59f",
        "a7c7bb4e68d2a2a797343bc452902d5530bae659a68e9910ed28e9358596f5be",
        "a7f2a5b46fca895f83a35998187e2ed8500532f8a448816ebd77a78987360c8e",
        "b46ed29f222d2a8472e293eb6050cbe1c0849f45e1512be2d81e8aad8c08e01d",
        "b70f7c9eb4bf35adee0ba6fb2c0a69d46d55e148186db2e9b6fbb244fe6ab672",
        "bd5840b923be3b798ba38eab22a580e8b435a7e1c833eda0d327d5e304f4e046",
        "be7558afc3db770c76e2622d90f81b62ad2371053eca1f44b8ce7bff679b9999",
        "c479c70d5d761a971c6aaee4a93dbd7945a257c6ecd70142759bbbb01dc06601",
        "c9d19e5a121d306628b70a9ba7de63ef3b9ef2ab4a47c2be1b3551540a695b0e",
        "cf566bbc1396d3572ce115eb33956aca90c33b04ee938544a04f4d5450bb7234",
        "d244bbeb9426b16b654a64e9d9320db2529d8cd9ec4d4601f0df6bb162d8b705",
        "e2a033f17dcff4535e433899ad57a42a924ec3701ea9cc2dabb92ea780f2c566",
        "e4ccd2160ad1d2cdd3b4b64e07e8b8190130213ccc23ae531c0e68d6cbbce007",
        "e6d512bf4f645bbd01be8de60bc67acdc3d39fd0fddcc7c835c93670b8577e37",
        "e912db89e385273540919a62324086b99e1e9c2e02e6b75180ef7ef4dfea0c45",
        "eba7dd572e962da4973373f3aa17eb99808296f647cde6bc54250f235f2767a5",
        "fc488cd54bde4a7d2041618980babc383f1d49242b09748b2e4cd4e3fa4df2e9",
        "ff045e78690626efde37b21308fb6d988f7ad2fc1ea2398d14187ac47e375910",
    },
    7: {
        "0220b12f0141d953fa71422aadf2b21e26b6bfe449ab28eec5262f51265fde05",
        "05bc75ae663551404651d442ecbe42a0b6193b8a45a22ec5dc0341024b0a35d1",
        "1c4e3fe7ec1afc769ecd804f4fc9ca17baa57d74094ad4cb8be142ea90fa3be8",
        "31e762835d71c10cdf16334022fb11dd2922c8b21757ae6cd3aca0830c475787",
        "3a477950c3af25bc77a7386031a6fa1e67c9d27a398c93555ce8bf23c146cdc4",
        "3b2df95b33f8de63f6c7ec1de78f0fb06e2b6bb117d7701ad721e762594d0fa8",
        "3b499c01ac491f120962037f8a164975dfba171401630a947d49420a336729b5",
        "3fe381fb027ba72b439f5a6f68450732b9b96af5152f4ccc8538e685e51e41c9",
        "45641e6c98107409c683d9ee334a8b64f1841a7f34c6e488c32be4729dae53df",
        "46c5a241f94379115a1c930617a8c10a2398186ffc42e0a19e1b3464a3711bf9",
        "5337c851b9cca3a35aeb0b7e70bf990d237e3b2266231420e4664b40afc5668a",
        "5545a4909a089708e053606d52a410f8c4de21ac1ff7d023fd8e5d5d0e31ef99",
        "55e46a0207c20902daeb836e5fbc9a64b062ac893dcac4e2030f7fc208bc699c",
        "60844fe5315934b85af1920f5210564a8f9deb803209d1fdb3ea64085945d302",
        "613bd58780b90f3a273311f01a7c22b43272252546966ff2290e13d936244663",
        "6a7f837af6c7ae2fae11f61cd6a61a19324f6d59af7581feef2c9f029631203c",
        "7bbbcd30cec982bdeb0420935d8e4591d1a1c831e1aaa700d92669bc76443355",
        "7e557e07e5e7aa74feae53682bc1db3ac7618ec11959da4056da1d189d44e704",
        "88e0723d5560f5c6e05840197acaf3e2aa2d47759dfd40ed00c9a7d1c4a18d22",
        "8e13e4f68524cacbc3a9f01feb5de49fe984afe84f138cc618b0f7dd39f15c70",
        "9272b912d9220d9473e60e66e1a05cbecac8d61b45f94495562d06e4aa9c2e6a",
        "932fb62cc6efae9abf46900e3519d9421f36681e3cd730b0fa3900d8e41e1a5f",
        "96259e40c4928e39fa76bd3301f65f48e9d314d93095335d93817a0559e277a0",
        "9625dc24810540acb0b3530177766642e2bf89c8cd8a16e2d72d4cc74597b47c",
        "9d807d0c0ff84b5005a74d74719bd62058f5ac366aaca30e137f195518879d8d",
        "9e52ed58bb0eb106b7e6c03ded033f197d3ee860030f2fa3b11d4aab7ade1b9d",
        "a491008981a5fd860689f7267fdf7290f7a1b85bf97848beaed2997175c9bf68",
        "a73140b927bcc1118e846d52bbb2be45722ab8e9fe963c16c56f6fbfd9865510",
        "bdda116a23a4d75785976a09115e14f037c79303ae98bbb658c7a58eab312d44",
        "c7bc31c00246a1a43799044f1dc2ef4b00f9300abd66fa0d6227b74c4884c867",
        "c8879be4a140a00eb3a8a70d68ce6aec4d5bc26936b13237d2ac9c3a1f203cc2",
        "ce884ef4632b06083976b2c81340d6a3a808babb523667733c25495775182a2c",
        "d82ce0f26b5ec789cb09649ed1ef5f2c6e14518947dd5ce5c22c5faf91d94768",
        "d9f39c9bf2b24e3fab24b6c45540b3fba568a112369acf78757759da286147e8",
        "dbf69399aea542dba647bdb50236de334c0bc564203d406dd3a9e50c7cf98af6",
        "fd95abe8c6caa3cc108773d6e5c422acbcbc7355744f9ba199b00f2b23c5928f",
    },
    8: {
        "0b788cde3261a9f9c6e2479dc088f0bdd9b9f100d0420f867c2c5776af95186d",
        "14e10f6060a19029cea9a284757bd47621f34746e251e108be31a34129a4d975",
        "1c48a01efaee0d99a82886c19a2e12488d2021765b8287355ede4e7efea879ee",
        "1dd67d993c6f492cca7beb26538c68e786db66d33ac906709e1be3faeac353a7",
        "2190ec76cec763af7b76624986276ac6d0c7074a577b6c06ddd35ff828ecd75d",
        "21a48a28f0edb8f26e536040c11b3f4ca6738eb58fe9851a334897cad40b7308",
        "4cff1adf5ac3c96d13205f2b448bcbef9d90afa88894586a4bd580622059f4b9",
        "53c004288aca0d76741983ef56f6e7953fca9a7607efd3fa7ba3a8c7e2ab978a",
        "58c348bcfbf426ed626e48024bb5966bb7e84816d7dd66bda979566a90094eaf",
        "59b300ab5150270eb9a5750fb2bbaadfa2043131bf5bf9759847bb593df0cba2",
        "825ba61dffbd0aa84765b1d0356a074708549817debfe2c5427ea1401697789b",
        "9d50fbd9961f0de834313699a618cb203ff59e481aa0005e82a828bb0ef59440",
        "a18577d59248e450bed92c174eaa2b1fdeacd73d7b417a819085dd8543c7b3eb",
        "a75d4f33b4af01d01fd18ae28433cd7568487d867e6d901596e07474831f548a",
        "a92900ebf38a4c0e7f1d79680b43055295f5bbd070b4b73e2cb969bdd266a5d3",
        "aa58b76004735f6962d8f032ec53a54812248bdf8223492732a3b7483d4aeec0",
        "ae51c6a99d3ed1106e1f0ad6246907e5f47a2bd19c19cf4da941ae73b8d45786",
        "af2f895604091125674c449710b3f6a1f8353e8644753a7a550e21b7eed13a6b",
        "b8b81d68d60cb63449f8c15b0b6479ca15abab74d5a31039b38912261947c384",
        "c090b440282f78c37a70e02aec51ab43f34ee6277c7e7257474ae5a80eef737a",
        "c4cefc20eb772b948373c3dafcc8a50ad613fe9b99118961150cf97dea7ed9f9",
        "c8fb0a912d39f5e60809603fa091e00df803815bf6afd35012f8857f1be93242",
        "cd05bf13c3960504299c8d7a2ee70d83f88f3c70d084da0adda03d281af07f1e",
        "dc49e004320a2b0d8168238cd102b97d5bb7f519b7ac0143963bc82bb3d1c248",
        "e857caa483a2da075d759770297c5288b0b7acad5c8a4d4b748bb9b4d0aa1aa7",
        "ee6efa658b3b98b2e9e2d96e173decc1268191cc3cbc3c9794505803bd8d91a7",
        "f48ae5cf9db390db05fac45184bb5419e7a79bf9afc42c5aa47e8f1a9d332a26",
        "fadb1ced50f59c10ce1ee2caa5fcd1f07fd28b62fa4ab6174c454d40cc455d53",
    },
    9: {
        "00d5bfbb6a51ba2281f8deeba5685d03e39e6fe8b738d7349ae81cede20b26c3",
        "04b04e10aff9b54181ff45b82ce27f0b02650e0db8e11b33105526f3e95fd5f7",
        "054ccefa42871af48e4887650f7e2e1a2ea00b5007e5c8a3f5f8c90a0ae124a2",
        "086b9d03c8d16df01b97caf199eec6f08fad84b6c530f9560b78f156c6a98e9e",
        "0d2006f9d3d9c56b0f838e19eb0977e14eeae818c6175593275544bf58639111",
        "10b4bd893456e8af7e3acb2fdcf1fda9ff769506b93c1c485710136bcc8aa1b7",
        "12dffefda0bf1f27dc30bb06d6bb8f4abdd8b5bffdea80740546a8e2433164bb",
        "17926bfed01f5c3cd46338a8e554361d53b20bafdc2d99412f9fbb09e414eb34",
        "19fa8577cad757706412540cccce62ac7b39488ce4df050580d9e7a618f39947",
        "1f4fa7025cca326fbec6f9b72f907c3dbe68a70232e156ed8f7a584b4abbd46d",
        "26d02568bd5c1cf5020424c58b4563b345c81c26792289970f251195fd627eaf",
        "3c058d03f44449aaa4ff7a5ffcd925dad8111d83ea97f78cebae986d1245b01a",
        "433af856930c2f9b778728ef989b17fae3fea697c04f7378eb02f6ddfb7a9c99",
        "48d1b7453b4a45c07eef1232aa98ff877fee2666814c7b82c89a1d45f7117444",
        "4b1927da01ddaddeb68aeecd66b3083f6e604f2b81302768d144e00e6ef23f7c",
        "4b19df8e5887fc2f64675db5068bc7b0bca6870b98486cd2c2294c4f7d2f9f4a",
        "4dd51593a6fef7af3dff711b2a95838ae9a1198353dbaab05999050af7794260",
        "674be7c13966cbe729f3225397521520d8baa0f815c05743ad351a46dcce95aa",
        "6b1b8fde3bdadea16530c83f0cc5b68a9c25084d8a3a6bd864677b6d556e3512",
        "6eda545adb200b8f3fcd5236e97beedf859b2e4be545295cf6eedaceb01448c4",
        "884e79afaf6542e44d7c43990ad2145d244c0d0a09e4d19e91b02ad8fb47368f",
        "8e0ba744794f4201878c59cfa230610f3a1fafb021995291f02408f7d52959fd",
        "972e4734213914c90665e94a3da99049c30f345a8f90fee6bbb3f07ab2282bc2",
        "9fbc079253443bf7ec717a64901b4e4b816aa9805f0ca04f5099fec661d1a223",
        "a02f3898a1070dcda4c2a7bd6ffc8dabc73fe40043bef4c3fa68b012787e16b2",
        "b69d38770fd3f23bfe6e8b5f7480ebb26863caa70e9dd7d38e82b5d139fa7f1b",
        "b98ccc51b5125ca851a1e24f0e299affb03356470a18aa12d4a9f5fab6d74c21",
        "be080189c2fdb4916c8bbda9d323957183e349f14648f04c3045b9054a6f321b",
        "bee598f3a5ea29d548edf38d2dd4cff82cd7e22044bc404edef291e3ff1126a1",
        "cf16f6a0eecf101394ba896a4d5328d2de7348ea27504f9053535a2f231be670",
        "d25cc8432dd31068c10725ab8aa28fed76d475fc57156841655d8f482d77b0f3",
        "d8b60c9f3e466f9b45408748cbdce6ba444ee40b82fc38b28444f8b2131ceada",
        "ddd50d2c13a7a5c40609283cb57f40862c38a7e0f34e3b5c411f65bd96f81927",
        "ffb7ffb4e96300798b68c0b222ca7ea4f61a88eebe6a9ca0fefe24666aecaa49",
    },
    10: {
        "08ac802fc3b17e1d184538396748d20ca96eb87fa0503638aefde2a9163036c9",
        "256301ca1a0fd273babc747ef49f2483b5ca39a49cf920cb1d1029a36dcf89eb",
        "3de4d2ebc2aec35c270f6c0d64fdc853e6cd5937725968b26c8bc1b6028ff82a",
        "41fe708afd52a9936cd5dce153aacb28a523f679d69ff8340076f64c1bca3b5a",
        "482d2f065e35b9e16fe149b7be954a6d6a64ea5ae13f139d9b7d64bd93c0116e",
        "652187bd0d785569e8421a9d79735580c2aff15b9e9b137db65927d85c80e448",
        "6ee086060549e7ee8fe7bcda9c14b24031bcff867d16995f778d2ac9e5dbc9df",
        "77f8186e3af60f929c9ac149d209037e323bd1125af1cf112a3c27fc6b10053b",
        "7e16d76b5ede1b9ec8a1bb96c9e5143949e03c7426f48fe56cd651a8f57f6e0a",
        "8e28d64945c0b985a353958f4cf297352b6e0ae09b0a1dee145529fac8b60fc9",
        "9a8e465889fb6e6b393fddc420f62b33bd8bd80699cb6809c21c8a8f26ad6ffe",
        "9e3133812df1e4ef4b6d192d95326a067f010c3f30a5c9c0e1af4b5d47339ad4",
        "a60384622e31955c3b24acabc0af5f450b3cf3edba57f876306636b7f825644e",
        "a86b2a78754c95cb6d47b93f102c16ea4bdfb834eee939951de9f353d1410c57",
        "aa8f11362cc8525f3fd2b3885097255b2cbc25aca5bd6442c0521891aadd09df",
        "b41bec2115ee3807594fb3604c356fb24b27c82210b78a88a8598f50ab1ab650",
        "b8302d81ae5331d07ef0a71c7461481fd5f8270c20e865d321a206ac967cc2a3",
        "b94c73828070678eae413dd45eae2ac52ceff51595946b1ba6954731565e03bc",
        "d1842105bbc510b113191906996c09488ede39a17545bf2bb16c1cab97ba3262",
        "d4d9a65afe29591b959333d2ac4ab36f5f0074c2d209d1cc894b2b9d2216a686",
        "db3c4eb31844d5db18c7bc192024cfe3e84f824c3511c5823047f61ac8930fe9",
        "de75cd9007dcf26a036978d44b7195595b40a4de5f2661d2456e2822dfe16a81",
        "e20083408d4e6f60c5007f84ce1d48a36b3175f56bdd24964a29142f17aae204",
        "e9aa8f9ff957dacf0073dcae1a3ea2099ba7ef70c0ddda05b0d2e943485a13c1",
        "eede779057205bdd075cc3c9a15f6e5989761fe401cc548d047a7e31dafe7b1a",
    },
    11: {
        "0c8323ea23f47b311ee564eee65d4381a10121fa8789f256150496e0fa7dbdb5",
        "27c743da7928658f90639272377b0ec24671ad306ce420d3276383429b9748b6",
        "445f3d8e3c263cbf8f9b7357101119718eaa97b55bc5983f90f217c73161c182",
        "555bee88bc51e02a7b5e117497a8f656f3b8f0bf9916be6328eabe8693a3edd1",
        "59532386dcaa40aac6e13f3c28005cbf7906872dc710a4bd497892750a49310c",
        "5c8bb18059dafd614d10b0e67c39558271c79ae356d710e921e92bd662b8fe62",
        "60913cb2438b97136432ef0e952c90fd28d65c082bc4aac7c0afccb706632f75",
        "6bc135dc29736d0686417e856d7f42faac39eea6aaf571f56483dd8cdbd7ae7e",
        "78b9c9aae10d131f23cb16eb9f7e6d7d5fd778ea96dadac8415a57d4497d69b7",
        "7c417619169bc42132412d8dc88663493241fdb27b718db92bc81f4cd0496a93",
        "8f7eead2cb4b8ba9605e09a9a89e78dce87519c92f6896845a0a627693b4ff4f",
        "9ab039b8f55a402e9209b6355d51a21bf884c0ef2d4e429953a9166b2f587f5a",
        "9d8c9e5d59d3eb7f16ceea252008a918aa648f8732a66667dd9558e82b8524a9",
        "9e17e29bd9b79d15c56d525b88248eb26e360aab10a8c19dd5488d906a5ff15d",
        "a01aa5d94c1a20bd31ece307d7d6d17e444e813f23585ce1f3cbd196e6956cd9",
        "af2e143c7950c3904588cc44b00a9ab8bb300d9ef6ca257d8a340a5816f494c9",
        "bb49615d7065233448dc07a098b3749ad117289a78ae61ef029c8108dec57e6b",
        "bd07c9e824c379e2f7c0a185576817c793f5307dcb89fc88658521aef06fe41b",
        "c04bc382d87f0d0d146b67050e5e5ac46e5b52b6f83a8e4fe7e58f63e3011ed6",
        "c6b3f6924be3ed38368e685094a3978c616293bcb365fa42fee7412170b820db",
        "ca3faa1b1d76dfc746d6c4f4dbf43a7eef30ec12aec3fad6d749c0c109acdfb8",
        "cc54f425c00e7b4748f96db713715c9a83b4e537ccceb1e09d43c31230d92038",
        "d5714ca81a8de3ac2e7ad6296be519dfde4e2e9af78a2eac16d3b2f02879279c",
        "d993d5a365c08eee1911bc4df3bfb101b3828c8ae2154bbbcbdf0bff5794a592",
        "f6a79c353be9de64b1bbe11d37f98eb2e757e9ab3e35cdd3b3a13ff14c328e5b",
        "f7d815d18a8160382d3dae539f9916811b3326903839b3e7fb4d45a3b8900d9b",
        "fc17022d92f94d55195421b24729e2bfd77b50c68a029574d19b11e8ac560295",
    },
    12: {
        "0449288cb469eabe991edbf0538e274df3ea4b22e0c091443d33582abc6d8f7d",
        "087155696b0a94de876835dadf4b85a89deb22a4b92a13ce6c83760a8d1969fe",
        "13985b3803b0cac696b2d4db71018bba0f73730865cd99c5744e8e0fcac758a1",
        "17d1f26afb6cb94ac100d53eedde6924bf1cbd0a0bd91ccf71a760bbc85e267e",
        "196dc86e93a3dbe5414201113c9e6f717c1da919a23ecf1f0b5f2ee412e47090",
        "293e9bbf9a703c802db1f465a3637fae76c9b3d8c027f86529977367e333db67",
        "32e5311e25d4ffd7680e88aea48bfaafa9f31e03348b9da55824d940a939f36d",
        "36f3739c6d3a045c22731d726a5ac713f364bac193656bb2704af313169a2925",
        "4296446863de17269c51e1cc3333fb196c6b8bb5222c6e0a54c89520de847d3e",
        "447bd2e1947015532a43409b109b5cb9df79a2f6700809f3c310f2433070a429",
        "474ff15ee66af70d869ff327e49bba8360efa745dc71dd0fabf3970c0a4a2743",
        "4add988c03174fac5f9599abaeb655a3b995b328315e11a280deb3f1710fb5b7",
        "4dac22d9b5bb09a71e615d79d9aa78ba0e9b13b083b2ab35c9d62bf9700b080b",
        "4f92b94aa4fe545c0bb2c874e7a01d8b373759316916565adc4729bb27580a79",
        "56c71800256dbd2e551ff9a824ca3824c0aef7b54911cc89afe63a2335a51b9d",
        "58242d36d2263b0a5a2e911ac7c6520c6dc3675ab2e5a7e8b0233f9040ac1a1a",
        "68f1ee43766844ce523067b8b5be3dfa074db7d870835368689498ab9addc325",
        "82079f1a2f724fb700b90dced546ba73adb4c437dc8ff99905741e31cfc9f0b1",
        "a934c4e62347cbf5c13648026ba8fdfb08e31b4d144775fd89df30024b3a2f54",
        "b4d7cf7a11e0b684de6870b1828f54cff7bf37c489c01c2a5d1b6cbf7d4b6ecc",
        "c6dfcd888676d4db6dbd8e98456a78a3a09e565a35c3ac5bd57ecd1d6e573806",
        "c74fa99aab07bae26f51c561086eea533ec47fec7ee257db9bc06cfe88f2d716",
        "cbf611cd7ad2c462728da954cd7582644be131e90959bdc8219bd65d7f64c039",
        "ce2f70d6eae732cd40cf8ee5b2cada375246ccc90ed289c85b11618818ade8ed",
        "e11412030bca1e5e79e53f0505c33a712b8f7be49b505913e8cda59e2d318c3f",
        "eb7a9752b5da793b7e3a68a9b69d3108cd213a275369838d7bfa4210b84f06e6",
        "f0dc288b506aa5f6994005853c2093b1c7fcb3336f81c9f3f66e8b6c25ebdbdf",
        "f43c811d93000c7160f1dd05facf70bead2ccfc14fbef5dd89865d0733b80f6f",
    },
    13: {
        "033aa24b5113d28374e84abe62d9f164b8969a2c25c4428f3d08ee6f934ad36a",
        "0bed2f49366e59060531966cb6199f7b41e89bec0abea3468e35e4d17ddc1abc",
        "0cc2005520db7c7bf9811620d8fb1209aa1428163a45ab13dd135a51a5d468b2",
        "1e4a858d38c07d523031850715e8848a9e76244f42b03e678ce73950f2665e81",
        "2a28e0df8db310eba2b631ff51b0bdc8776632a08a3991be27b3d8272021b067",
        "33eafe6a0ee18549dad23a318c87577e70b5b08a6ff3c46fb0dba01559b7fd21",
        "4333e2442ee77f665e7679ba466c18dd410abef04aa99f9f09fc6f7d9e2a19be",
        "4355051c229e61527e166fec60a2f22850c33de98f7261238ebbd610c5c70f88",
        "44cf477538a43b20c59ffdb8b4f2d56b0eccf78f05df0a9e2f6a5302cbc9cd36",
        "5380d89c2a7044832564b4e3587db71ca445fb36b9132c97783e953ac207b13f",
        "6743baae88b12dce324345cfa47fdc7f271c7f46801d5bae4607b681d68cbd25",
        "6acb6ef61d865fb571364ad4e43480c9b61429e40a5f6a54f8a29d1b65523dfe",
        "7a4acd4afe79b0f5b8186f0f523786be20bbdd25976120589b929776ee5efd68",
        "7c3cbba92cdd391a7ce6e41c5a5ff366e4d8e49e92516d01cfe8e13d2f110a7b",
        "8b9c67da22eb3bc63744657bd1805196c90ac2d7e9c58e2d8e3415c6b1491c86",
        "91a669934bfc98bf0900ce98d91acda787ab23e387ebcc00b10621b0f32fb5f3",
        "95f41941d962931466c6274a2576f7438e19d152430861bd3adbd9d2f49e692f",
        "9e416a15ecfa0417d0b4e56fe898b53e187d5abe9e2d053e792e63b179ccfdf8",
        "9f58efe51ce05f21996d10ad1fcac5d3a06088e3eb3c6c949d127a3489e1b24a",
        "acb3c461e310b7929907746c2966778d20674e38656711da7c3718750dac3938",
        "b8a8a65b79010027f7fc5834d057722ce300998391ae888dd6888e4affd925cc",
        "bd06119aee464bd9de009f37cd360823881c31306c0337cc4b9f1a54ef1a6634",
        "c2c7b0b22c1ebdb25b0b7af99910e6d8c7ff2b4e54a8215dcd9fdae5a88c0137",
        "c976ed2dbe7dabed7d886e0e52c5f535a4ae510ccbe9e45a36a8b9863515b3a3",
        "cc5dacc458a98647f21faeb52b29a10170f272940e37983ae0a6ffa489152257",
        "d2077726e7b90427466d3895f502597dea72c9ba012f3bdc38046899d5462af4",
        "d5b0bd1cab2821c125f94a201243cc5be4c790668b193b325f23e78ce5669266",
        "f0fd92f380c7f1e97054f9c1149ccff7b439e0c7965fa500d7ea69c1848cf55e",
        "f14554b089a7bd366a33ed07f43fd97158f175591ff4fd0cc1d767c61dce89b3",
        "f67186a48432be1933fc0961e74865fd4d50720ba37023c30a7d9afbd3d26ba2",
        "fad739f1fb4b266bfe8f1a6b5106b34dfde9166956038fad76539a427dd8f17e",
    },
    14: {
        "1fb224b2076bc73651fc98f5aa633b210d4bf21a097373864fadeead1aaa2b34",
        "df020cce0dfa6f06d34a2d3bbcc716166804139d87c0e5480c6b822a3737b4ce",
    },
    15: {
        "ddb8cf8c7978d30d922806553164f2a3acf51f616e21412754fd97b3b0b9f85f",
    },
    17: {
        "20b28c39cee18671f57a34741c31321373b913ae106825504c95216e47ddde21",
    },
    22: {
        "de6459ec1c497777d422ba2653661d231e836b07daac38fbb1a154cd8d381c1a",
    },
    35: {
        "41cb68d645322e32a8ba9fe1b60277972da194d1122f8c93c83a258f78b811f1",
    },
    37: {
        "5cdf3f46a61afc84b320c302fe125724ff40038fe4d6e4bf7cb5f4e09c6937e1",
    },
}
FOLLOWUP_FIGURE_DIGESTS = {
    2: {
        "2dbd9afb3bb90d68e38782e1580be725d6e3784c8dba50593560bd8992e56850",
        "2e45f50a2d75e87d7897213692ee17fdec3ee589b21e44ea483b17a2a96e30ed",
        "72e6436adfda0a8c31dd790e2384f892d605d76662a7bd1650513f6ec591d930",
        "d1ce334b6a7f364b1e97ebb9e661f6d4cf6e20aa741aba4b8f198417aea18326",
    },
    3: {
        "062368cd48903afc05a3955f2a1104928526e99fc7dc39047297ccf094b8c709",
        "0ae35e08eeb29b67be5d3761b4e37e1a3bee75b7deaa52ff76b2763d4c02d3dc",
        "28fdae8deae31d6eafd18b70d878f6d8a3136f267ce273777c089a42ac590438",
        "29bdb54f41e32a4a1c67d8c174e760893cc5cd59ba8a3c1da40ebd081564d755",
        "3ccce1f4da948035fd36a399576a50a5248ddc02dd008f3c3668df57c0aa75b5",
        "412a4789b02cad19bacb029f5c8ec8e9b115375d82b97df1d6b15997a8e70e01",
        "43d7d579df6125cb8a7ae6a65d902d2d91985ad48dca7efbf283877623a771fc",
        "5233243e2ade369857ed45dea4456efd35c91c28a499179e6a96d4e43b8a9668",
        "66bb78e8a32c003913284de62ad9ed06132c3f1884bea454c1e0cf9534ddd63a",
        "709b082ced7f444e03973c02f22c8cfcc04408b64d82e859195cfd5a6d659419",
        "821343c8782b17473fc0ec391a7c2d66f648be47cb71d8455f81ebc84dd9f93c",
        "883caeb7ca1165a22441d8487203cc8aa9284ad7c0f5664c704f98ecb10849b4",
        "8e465056c8c3e5e558b5d116b313bcfa266f137049ea0a23a590766dbcbf14bd",
        "96572d9b742413047fc63788c553e3ea36c205a93f0191635c8a9c9f518a23a1",
        "cdb0a2d239cdf833c69cd53f4e9a88066ebc0a35e97a57df44fb5b209efd56e5",
        "d1552596b1c462764a3f0be84cbfd5a6e42cca1b19e6f1764f1faf5dde83a8bb",
        "d5d4de85499a2e0223d13c99bcf8499310c085a16193c3b81ae0ca016d2daf4d",
        "e4f652d7f6915101792db46b8c9e1a42f2164e5257def11160f9910141660470",
        "f7bf518e5a25b3a397f789f3397757b0e5cf023db4dec67cbb53262289ab6e66",
        "fd93cd49a8f695355cf4b94405b65491bbaa259def883d3fc515e116808fe0ab",
    },
    4: {
        "0363ceb0b2389aec71e6077fba7ee268e38c46111acdf199cea079bbbbbcc0da",
        "1d8a70253ecfbd012018fb8d8882da220a8d76f3c1f41a0825b53058608554f3",
        "1f1c34f2ae1785879c1b4446840fabd7160f7cf7d893ab48f442cce613d8b1a0",
        "631444d2bb9e1068d654c5f891590b83dc1ef60a2f3c6173e272c672ec61ced6",
        "97945fd39c62e84a6ee7763d45171642b990e6a56b77cf2cc88fb75e17c79d0b",
        "a79e143fd087110805792ea974418cade52ec54b4e78b5407a3797272a7ef2ab",
        "ae0ccc88b786261806c0c969ade49ceeb64a7cdb8a0fc78d8cb7e4475c0e3928",
        "c43771c4081257be4bfcbcc3513950749562ddbc30b90dc9e2a885ab3e078022",
        "cd2091ecd0c5c035d5a137fc4bea09f02485594e7440017e644192d0adb6914e",
        "d29c06a03658cdbd1f45b5aba28eb4f6ca9832fb4dfb2f718cd7b69b8434e651",
    },
    5: {
        "022c6e47994e422f3e8ca1cb0ef7d6fc8a9f1f34a08d960b0d147b6f54d34092",
        "14e7bc49925cf5dc4a07f19e6bc0d533aa37c5d580739f15c19aa3d7cd70a5d1",
        "17bab9b56516b39eb5b2af7e36e061b96794162d21e6ac63cba7f77a27f84228",
        "23ee6c74cec0d3e080156b16fc36b3b87bc7be4d216d55fa2f0ec1ccaedb22f8",
        "42c970c7a723a4998328881ff6033de7051fd2934a421f11556e3b2c5d7636ce",
        "4b5728561e60dd727547293092a9acd5aa2e7cd7ebbea99a303931c0c83c80aa",
        "66dbae68e4f498ba668b52b481891465277f250cd3cb3ced6c14144dbfae52fb",
        "68c8dd1b697d5ab90981e2b1c9fb4b40e27ac763a4051d0f64d0dbd07eaf70ac",
        "6cbd14c90d06f49900f01240cf47309442e6eddfcaef35ddae628d6dd2584926",
        "6ebcd2bba0abbe420cd07cd5f2b4821f8a852af9d7b8a268db3e2ebf650c6594",
        "7ac430018c04dac4d96ad6edae62b995b52bad3007b85e4b85e6096434bc53bc",
        "86552f7899779cb8b460e07acaa45a994ee2ece1a6772d22fef05bbbf93ef33f",
        "a793d510adeab207a1b873b8fb6aacc19bd980a0c90f09cf6a3d6ce4f90423a4",
        "b2008c37ee3a7cf7ba87f5ad50dd9e1120234bc8d8046c2509de1760fa9fa7f6",
        "bc744c4ff62c992caebba2b01bbb820791ccedf62229613312a2f7774731bff2",
        "c766f63f678423807886bbc62ad77f70a65c33b724232958cc97b4098fe8c24d",
        "c9e027f325858c52d3e2464d5ee08c4c74860810a4f14a5dccc16ec63b18d3bc",
        "cee2fb40e8210e989ac9163784f73dd2ffb6560d1ba73e6b1e5ac1936b5c69db",
        "eaba2fc8b501189b8e9abfa2e0feab57e75819331a3bb7886e11a41ca0f9ec19",
        "ecda9abd3d56484286b2af54507126386b5e598ac9eb20dea4a3c1f7687e9f0c",
        "ef268ccd65bf2ff1ac43609025e0c2cc8ee6aa6e395e67c48ce788558165de9f",
    },
    6: {
        "195ab278f8114427c3bbfe78f79ef49460d3ffac74321c4e3e9d5b15b0fa2b4e",
        "22cf7c1efa04613602d670eed872d4d3f9070e0e67ac744a6c713aec9bacdda7",
        "422f240e43a3226f329ba4a0236f587c3f2226e05c511c8d9151b2320473a1a8",
        "43d284bbbed3c528f2b1e771e52c04e631162e98a970993cec26afcba942e691",
        "6845e5c1c695991b35264ed80ae155cebe670183180874ab4c7b8ec90e712cea",
        "b2ee3509c1fa4f9741f894e592bda9acca86020918d19f02d8e644fe40d58e6e",
        "c548c333551c5cab15b675354701a0f54963135d7ebe5e3bf40187c7940aa4c0",
        "f16f6e530dda2f732318c9195ed1023cbf464a58c4db129b04dcafe1ef14d36c",
    },
    7: {
        "4b85faf001d74d27146ed9d157f1bdc5d9e1b92482f7f23cc05e1d24edccc9f4",
        "62b58e642d164d0464d3b36c4d6b4c884a5207961f2daea94476789443547be8",
        "6f6def8dc39d6739f3f78dcad62668cedc0fbefa658b924c6bdf852662fdc96b",
        "c2e2be222588fdd1a12a63954a1d54e2009b91f40a36c69bbac1672730bebfce",
        "dbc38fb17a681033b80d6aa9e4bd306e8a95e428b11c2e865e58fbcc1ff54004",
        "ea23ed15127af3861842c492076dae4e99b05a4f17f888fb3ae07c82be17393a",
    },
    9: {
        "18b3d14ade1b23572c8d9d22267238312dc9fb1f820e001ec4741a6716e8ae82",
        "41bc1b2bba243879c158f57d685a8d5df40bcd5a26b0f10bddf8b22f54a2d6cd",
        "68156a533f8d4d0fc05e77fb6eab655bd93f72f0bddc686ef808ce21242e8ced",
    },
    10: {
        "2a1c3e19b11cdae175bb50df8b11cd8752caefd293bfe23d389342b4be6b99d3",
        "3802c22072dab9dd9c440e3d0eaf49ef874dbfc7818cc7170a18c456af3759d0",
        "4283e109f0d4987db8cefad046fd9114d2d77961779fdbd2a8f77934e9e57402",
        "778c31b736ad166fe9c05f7c785e40775ccec9b10d03ffc32b1fe55d46559c1c",
        "82f05bfd481da346d932944358a83329866ddbc9a2ad92e2fc35fa2e3e72caf2",
        "ad8ac746bcf097644da167935e4b6ca56819bca7a6aeb6cec02845ab8612e09e",
        "da3b1c4025959abb18818ae11affb5b128fd78b538a4dd09c0a521d0d9023ed3",
        "f0a7f07f2cc0d7fa8260c9389d43199c0ade8513762339d63539b15b027b372c",
    },
    11: {
        "13fb1c8ee54ff75727300a8d801e0e60d4ac84256e1b9233eea71af909521dba",
        "1eea3c60d9d15f477d76b4c25bb9d5f07209f356f49f2a2cf5ca59b5ae5ae62f",
        "256266184a34d960c036b35122572455dc2b522f33ef9dd8cb7808856f14a82e",
        "4ab2001dd1af1c42540b8f57d5cf7af79019086e598839c5a6fa69ed26588ec0",
        "4ecfc203c6724390b432a1db5dac44556ca64fee8e39795e46fa7ad607c449d9",
        "d03dd588d6c59361dc7d160e0d359313777663887ee59ab9affec62b04e795cb",
        "e2c286ab0a7051ac49d6dcf545d4d0e02d1470b35b286568fd72e31dbf382f91",
    },
    12: {
        "5454f0b25ee6d75297ee644fa565889717b02ad680ce5d4e6316abef1f4fb08d",
        "78d10c90deca14515523ed474d70dd9f641bb6f045b6ca8b593b6b2e199e85fc",
    },
    13: {
        "162d2641d6472d99dca7d7a50705a78a9d9fee95f15421bac02c4fa633b2c98c",
        "78e9dfdd675536f5be7d47c1a7f59d9cd67b8aa7fd545ee9fbd8614f5e31d11f",
    },
}
FOLLOWUP_TABLE: dict[int, set[str]] = {}
for _table in (FORBIDDEN, FIGURE_DIGESTS, FOLLOWUP_FIGURE_DIGESTS):
    for _length, _digests in _table.items():
        FOLLOWUP_TABLE.setdefault(_length, set()).update(_digests)


def normalize(text: str) -> str:
    """Invisible characters removed, the fullwidth percent sign read as %, no-break and thin spaces as spaces."""
    return text.translate(INVISIBLE).replace("\uff05", "%").translate(SPACES)


SPACED = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), " ")


def fold(text: str) -> str:
    """The phrase scan's forms: invisible characters removed (a phrase split inside a word) and read as spaces (a
    phrase joined by one), each with hyphens and dashes as spaces, whitespace folded and lower case."""
    forms = (normalize(text), text.translate(SPACED).replace("\uff05", "%").translate(SPACES))
    return "\n".join(" ".join(form.translate(DASHES).lower().split()) for form in forms)


def files(globs) -> list[Path]:
    found = set()
    for pattern in globs:
        found.update(path for path in ROOT.glob(pattern)
                     if path.is_file() and "__pycache__" not in path.parts and path.name != "LICENSE")
    return sorted(path for path in found if str(path.relative_to(ROOT)) not in SCAN_TABLES)


class FollowupBlindnessTests(unittest.TestCase):
    def test_the_scans_see_the_follow_up_files_tests_and_fixtures(self):
        names = {str(path.relative_to(ROOT)) for path in files(FOLLOWUP_GLOBS)}
        for required in ("src/ocsl_review/followup_schema.py", "src/ocsl_review/followup_branch.py",
                         "instruments/citation-function/1/system-prompt.txt", "docs/followup-audit.md",
                         "skills/ocsl-review-followup-audit/SKILL.md", "schemas/followup-protocol.schema.json",
                         "tests/followup_fixtures.py", "tests/test_followup_pipeline.py",
                         "tests/fixtures/followup/README.md", "tests/fixtures/followup/status/retraction-watch-snapshot.csv",
                         "tests/fixtures/followup/tables/synthetic-statistics-tables.json"):
            self.assertIn(required, names)
        self.assertFalse(set(SCAN_TABLES) & names)

    def test_follow_up_files_carry_no_study_identifiers_or_design_figures(self):
        for path in files(FOLLOWUP_GLOBS):
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual([], forbidden_hits(normalize(path.read_text(encoding="utf-8")), FOLLOWUP_TABLE))

    def test_no_tracked_file_outside_the_census_tests_carries_a_design_figure(self):
        scanned = {path for path in tracked_files()} | set(files(FOLLOWUP_TESTS))
        self.assertGreater(len(scanned), 50)
        for path in sorted(scanned):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual([], forbidden_hits(normalize(text), FIGURE_DIGESTS))

    def test_follow_up_files_name_no_earlier_work(self):
        for path in files(FOLLOWUP_GLOBS):
            text = fold(path.read_text(encoding="utf-8"))
            with self.subTest(path=str(path.relative_to(ROOT))):
                for phrase in PHRASES:
                    self.assertNotIn(phrase, text)

    def test_lab_facing_text_and_follow_up_modules_carry_no_figures(self):
        for path in files(LAB_TEXT_GLOBS + FOLLOWUP_SOURCES):
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual([], FIGURES.findall(normalize(path.read_text(encoding="utf-8"))))

    def test_the_scans_detect_hidden_and_respelled_figures_and_phrases(self):
        import hashlib
        table = {7: {hashlib.sha256("4,321 %".encode()).hexdigest()}}
        for spelled in ("4,321 %", "4,321\u00a0%", "4,321\u2009%", "4,321 \uff05", "4,321\u200b \u200b%"):
            with self.subTest(spelled=spelled):
                self.assertTrue(forbidden_hits(normalize(f"a share of {spelled} here"), table))
        for text in ("a share of 3.5 % here", "3.5 percent", "3.5 per cent", "3.5\uff05", "in 1,234 reviews",
                     "in 1\u2009234 reviews", "in 1\u00a0234 reviews", "in 1 234 reviews", "3\u200b5 %"):
            with self.subTest(text=text):
                self.assertTrue(FIGURES.search(normalize(text)))
        self.assertFalse(FIGURES.search(normalize("citation-function@1 and S0-S3 in 2025, units 12 and 345")))
        for text in ("the earlier\u200b census", "an earlier-census", "the earlier\u00adcensus", "a Prior Run's",
                     "the previous\nstudy", "a past census"):
            with self.subTest(text=text):
                self.assertTrue(any(phrase in fold(text) for phrase in PHRASES))


if __name__ == "__main__":
    unittest.main()
