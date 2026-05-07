import requests
from typing import Dict, List, Any


PUBCHEM_VIEW_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON"


# -----------------------------
# 1. Fetch PubChem data
# -----------------------------
def fetch_pubchem_data(cid: int) -> Dict[str, Any]:
    url = PUBCHEM_VIEW_URL.format(cid=cid)
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.json()


# -----------------------------
# 2. Recursively extract safety sections
# -----------------------------
def extract_safety_sections(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keywords = [
        "safety",
        "hazard",
        "toxic",
        "toxicity",
        "health",
        "exposure",
        "first aid",
        "warning",
        "handling"
    ]

    results = []

    def walk(nodes):
        for node in nodes:
            title = node.get("TOCHeading", "").lower()

            if any(k in title for k in keywords):
                results.append(node)

            if "Section" in node:
                walk(node["Section"])

    walk(sections)
    return results


# -----------------------------
# 3. Extract readable text
# -----------------------------
def extract_text_from_sections(sections: List[Dict[str, Any]]) -> List[str]:
    texts = []

    def walk(nodes):
        for node in nodes:
            infos = node.get("Information", [])

            for info in infos:
                value = info.get("Value", {})

                # Case 1: plain string
                if "String" in value:
                    texts.append(value["String"])

                # Case 2: structured string
                if "StringWithMarkup" in value:
                    for item in value["StringWithMarkup"]:
                        if "String" in item:
                            texts.append(item["String"])

            # recurse deeper
            if "Section" in node:
                walk(node["Section"])

    walk(sections)
    return texts


# -----------------------------
# 4. Main pipeline
# -----------------------------
def get_safety_profile(cid: int) -> Dict[str, Any]:
    data = fetch_pubchem_data(cid)

    with open("data.txt", "w", encoding="utf-8") as f:
        f.write(str(data))

    record = data.get("Record", {})
    sections = record.get("Section", [])

    safety_sections = extract_safety_sections(sections)
    safety_text = extract_text_from_sections(safety_sections)

    return {
        "cid": cid,
        "safety_sections_count": len(safety_sections),
        "raw_safety_text": safety_text
    }


# -----------------------------
# 5. Example usage
# -----------------------------
if __name__ == "__main__":
    niacinamide_cid = 243


    result = get_safety_profile(niacinamide_cid)

    print("CID:", result["cid"])
    print("Sections found:", result["safety_sections_count"])
    print("\n--- Safety Data ---\n")

    with open("toxicity.txt", "w") as f:

        for line in result["raw_safety_text"]:
            f.write(line + "\n")

         