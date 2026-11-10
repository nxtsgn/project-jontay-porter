"""Team-name normalization and the conference filter for college sports.

Conference membership is for the 2026-27 season as best known (realignment through mid-2026). VERIFY each season:
Gonzaga moved from the WCC to the Pac-12 for 2026-27; Oregon State and Washington State's WCC affiliation ended; Notre Dame
is ACC in basketball and independent in football; the user asked to include it in both. Gonzaga (now Pac-12) is included
for men's basketball only, and Grand Canyon for both basketballs, at the user's request.
A college game is collected when at least one team is in the user's conferences.
"""
import re
import unicodedata

SEC = ["Alabama", "Arkansas", "Auburn", "Florida", "Georgia", "Kentucky", "LSU", "Mississippi State", "Missouri", "Oklahoma",
       "Ole Miss", "South Carolina", "Tennessee", "Texas", "Texas A&M", "Vanderbilt"]
BIG_TEN = ["Illinois", "Indiana", "Iowa", "Maryland", "Michigan", "Michigan State", "Minnesota", "Nebraska", "Northwestern",
           "Ohio State", "Oregon", "Penn State", "Purdue", "Rutgers", "UCLA", "USC", "Washington", "Wisconsin"]
BIG_12 = ["Arizona", "Arizona State", "Baylor", "BYU", "Cincinnati", "Colorado", "Houston", "Iowa State", "Kansas",
          "Kansas State", "Oklahoma State", "TCU", "Texas Tech", "UCF", "Utah", "West Virginia"]
ACC = ["Boston College", "California", "Clemson", "Duke", "Florida State", "Georgia Tech", "Louisville", "Miami", "NC State",
       "North Carolina", "Pittsburgh", "SMU", "Stanford", "Syracuse", "Virginia", "Virginia Tech", "Wake Forest"]
BIG_EAST = ["Butler", "UConn", "Creighton", "DePaul", "Georgetown", "Marquette", "Providence", "St. John's", "Seton Hall",
            "Villanova", "Xavier"]
WCC = ["Loyola Marymount", "Pacific", "Pepperdine", "Portland", "Saint Mary's", "San Diego", "San Francisco", "Santa Clara",
       "Seattle"]

FOOTBALL = SEC + BIG_TEN + BIG_12 + ACC + ["Notre Dame"]
# Grand Canyon (Mountain West) added at the user's request; GCU has no football team, so basketball only.
BASKETBALL = SEC + BIG_TEN + BIG_12 + ACC + ["Notre Dame"] + BIG_EAST + WCC + ["Grand Canyon"]  # women's
MENS_BASKETBALL = BASKETBALL + ["Gonzaga"]

# venue spellings -> canonical (after norm())
ALIASES = {
    "mississippi": "ole miss", "miami fl": "miami", "miami florida": "miami", "miami (fl)": "miami",
    "north carolina state": "nc state", "nc st": "nc state", "pitt": "pittsburgh", "cal": "california",
    "connecticut": "uconn", "brigham young": "byu", "southern california": "usc", "central florida": "ucf",
    "texas a and m": "texas a&m", "texas am": "texas a&m", "tamu": "texas a&m", "louisiana state": "lsu",
    "southern methodist": "smu", "texas christian": "tcu", "saint johns": "st johns", "st johns ny": "st johns",
    "saint marys": "st marys", "saint marys ca": "st marys", "st marys ca": "st marys", "lmu": "loyola marymount",
    "loyola marymount lions": "loyola marymount", "seattle u": "seattle", "seattle university": "seattle",
    "boston col": "boston college", "bc": "boston college", "penn st": "penn state", "va tech": "virginia tech",
    "ga tech": "georgia tech", "west va": "west virginia", "wvu": "west virginia", "fla state": "florida state",
    "fsu": "florida state", "gcu": "grand canyon", "grand canyon university": "grand canyon",
    "grand canyon antelopes": "grand canyon", "osu": "ohio state", "unc": "north carolina", "uva": "virginia", "ucf knights": "ucf",
}


def norm(name):
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\((w|women)\)", "", n)
    n = n.replace("&", " and ").replace("'", "").replace(".", "")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\bsaint\b", "st", n)
    n = re.sub(r"\bst$", "state", n.strip()) if not n.strip().startswith("st ") else n
    n = re.sub(r"\bmich\b", "michigan", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = ALIASES.get(n, n)
    return n.replace(" and ", " and ").replace("texas a and m", "texas a&m")


FOOTBALL_SET = {norm(t) for t in FOOTBALL}
BASKETBALL_SET = {norm(t) for t in BASKETBALL}
MENS_BASKETBALL_SET = {norm(t) for t in MENS_BASKETBALL}
