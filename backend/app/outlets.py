# backend/app/outlets.py
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import get_close_matches
from urllib.parse import urlparse

_REGISTRY = """
thehindu.com|The Hindu
ndtv.com|NDTV;NDTV News;NDTV India
indianexpress.com|The Indian Express;Indian Express
timesofindia.indiatimes.com|Times of India;The Times of India;TOI
hindustantimes.com|Hindustan Times
indiatoday.in|India Today
news18.com|News18;CNN-News18
thewire.in|The Wire
scroll.in|Scroll.in;Scroll
theprint.in|ThePrint
thequint.com|The Quint
firstpost.com|Firstpost
livemint.com|Mint;Livemint
economictimes.indiatimes.com|The Economic Times;Economic Times
business-standard.com|Business Standard
financialexpress.com|The Financial Express;Financial Express
moneycontrol.com|Moneycontrol
deccanherald.com|Deccan Herald
deccanchronicle.com|Deccan Chronicle
thehindubusinessline.com|BusinessLine;The Hindu BusinessLine
telegraphindia.com|The Telegraph India
tribuneindia.com|The Tribune;Tribune India
thestatesman.com|The Statesman
newindianexpress.com|The New Indian Express
mid-day.com|Mid-day;MID DAY
freepressjournal.in|Free Press Journal
republicworld.com|Republic World;Republic;Republic TV
timesnownews.com|Times Now
zeenews.india.com|Zee News
abplive.com|ABP Live;ABP News
aajtak.in|Aaj Tak;AajTak
indiatvnews.com|India TV;India TV News
dainikbhaskar.com|Dainik Bhaskar
bhaskar.com|Bhaskar
dailythanthi.com|Daily Thanthi
amarujala.com|Amar Ujala
jagran.com|Dainik Jagran;Jagran
livehindustan.com|Hindustan;Live Hindustan
navbharattimes.indiatimes.com|Navbharat Times
patrika.com|Rajasthan Patrika;Patrika
jansatta.com|Jansatta
lokmat.com|Lokmat
eenadu.net|Eenadu
manoramaonline.com|Manorama Online;Malayala Manorama
mathrubhumi.com|Mathrubhumi
anandabazar.com|Anandabazar Patrika
dnaindia.com|DNA India;DNA
oneindia.com|Oneindia
outlookindia.com|Outlook India;Outlook
theweek.in|The Week
wionews.com|WION
cnbctv18.com|CNBC TV18
ndtvprofit.com|NDTV Profit
asianage.com|The Asian Age
siasat.com|The Siasat Daily
thenewsminute.com|The News Minute
newslaundry.com|Newslaundry
swarajyamag.com|Swarajya
opindia.com|OpIndia
ptinews.com|PTI;Press Trust of India
aninews.in|ANI;ANI News;Asian News International
ians.in|IANS
uniindia.com|UNI
newsonair.gov.in|All India Radio;AIR News
pib.gov.in|PIB;Press Information Bureau
altnews.in|Alt News
boomlive.in|BOOM;BOOM Live
factchecker.in|FactChecker
bbc.com|BBC News;BBC
reuters.com|Reuters
apnews.com|Associated Press;AP News;AP
afp.com|AFP;Agence France-Presse
nytimes.com|The New York Times;New York Times
washingtonpost.com|The Washington Post;Washington Post
wsj.com|The Wall Street Journal;WSJ
ft.com|Financial Times
bloomberg.com|Bloomberg
theguardian.com|The Guardian;Guardian
telegraph.co.uk|The Telegraph
cnn.com|CNN
aljazeera.com|Al Jazeera;Al Jazeera English
npr.org|NPR
nbcnews.com|NBC News
abcnews.go.com|ABC News
cbsnews.com|CBS News
usatoday.com|USA Today
foxnews.com|Fox News
politico.com|Politico
axios.com|Axios
time.com|TIME
newsweek.com|Newsweek
independent.co.uk|The Independent;Independent
news.sky.com|Sky News
dw.com|DW;Deutsche Welle
france24.com|FRANCE 24
euronews.com|euronews
scmp.com|South China Morning Post
straitstimes.com|The Straits Times
channelnewsasia.com|CNA;Channel NewsAsia
japantimes.co.jp|The Japan Times
dawn.com|Dawn
thenews.com.pk|The News International
geo.tv|Geo News
tribune.com.pk|The Express Tribune
thedailystar.net|The Daily Star
kathmandupost.com|The Kathmandu Post
smh.com.au|The Sydney Morning Herald
abc.net.au|ABC News Australia
cbc.ca|CBC News
globalnews.ca|Global News
trtworld.com|TRT World
cnbc.com|CNBC
forbes.com|Forbes
businessinsider.com|Business Insider
snopes.com|Snopes
factcheck.org|FactCheck.org
politifact.com|PolitiFact
"""

WIRE_DOMAINS = {"ptinews.com": "PTI", "aninews.in": "ANI", "ians.in": "IANS",
                "uniindia.com": "UNI", "reuters.com": "Reuters", "apnews.com": "AP", "afp.com": "AFP"}


def _norm(s: str) -> str:
    s = re.sub(r"\(.*?\)", "", s.lower())
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s[3:] if s.startswith("the") and len(s) > 6 else s


_DOMAINS: dict[str, str] = {}
_BY_NAME: dict[str, str] = {}
for _line in _REGISTRY.strip().splitlines():
    if not _line.strip() or "|" not in _line:
        continue
    _dom, _names = _line.split("|", 1)
    _aliases = _names.split(";")
    _DOMAINS[_dom] = _aliases[0]
    for _a in _aliases:
        _BY_NAME.setdefault(_norm(_a), _dom)
    _BY_NAME.setdefault(_norm(_dom.split(".")[0]), _dom)

_SLD = {"co", "com", "org", "net", "gov", "ac", "edu"}
_TITLE_SEP = re.compile(r"\s+[-|–—•·]\s+")      # needs surrounding spaces: "Mid-day" survives


@dataclass(frozen=True)
class Outlet:
    name: str
    domain: str     # "" if unresolved
    via: str        # xml | registry | fuzzy | title | unresolved


def outlet_key(o: Outlet) -> str:
    return o.domain or "name:" + _norm(o.name)


def split_title(title: str) -> tuple[str, str]:
    parts = _TITLE_SEP.split((title or "").strip())
    if len(parts) >= 2:
        pub = parts[-1].strip()
        if 0 < len(pub.split()) <= 5 and len(pub) <= 40:
            return " - ".join(parts[:-1]).strip(), pub
    return (title or "").strip(), ""


def _registry_domain(host: str) -> str:
    parts = host.split(".")
    for i in range(len(parts) - 1):                 # most specific suffix first
        cand = ".".join(parts[i:])
        if cand in _DOMAINS:
            return cand
    return ""


def _registrable(host: str) -> str:
    p = host.split(".")
    if len(p) >= 3 and p[-2] in _SLD and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def _lookup_name(name: str) -> tuple[str, str]:
    n = _norm(name)
    if not n:
        return "", ""
    if n in _BY_NAME:
        return _BY_NAME[n], "registry"
    if len(n) >= 6:
        m = get_close_matches(n, _BY_NAME.keys(), n=1, cutoff=0.9)
        if m:
            return _BY_NAME[m[0]], "fuzzy"
    return "", ""


def resolve_outlet(source_name: str = "", source_url: str = "", title: str = "") -> Outlet:
    _, title_pub = split_title(title)
    name = (source_name or "").strip() or title_pub
    host = urlparse(source_url).netloc.lower().split(":")[0] if source_url else ""
    if host.startswith("www."):
        host = host[4:]
    if host and not host.endswith(("google.com", "googleusercontent.com", "bing.com")):
        d = _registry_domain(host) or _registrable(host)
        return Outlet(name or _DOMAINS.get(d, d), d, "xml")
    for cand, via_title in ((source_name, False), (title_pub, True)):
        d, via = _lookup_name(cand or "")
        if d:
            return Outlet(_DOMAINS[d], d, "title" if via_title else via)
    return Outlet(name, "", "unresolved")      # kept as a distinct outlet, never rejected


def _resolve_outlet_domain(source_name: str = "", source_url: str = "", title: str = "") -> str:
    """Drop-in: returns the domain string ('' if unresolved)."""
    return resolve_outlet(source_name, source_url, title).domain
