import json
import glob
import os

def analyze_nphies_folder(folder_path):
    print(f"Reading files from directory: {folder_path} ...")
    
    json_files = glob.glob(os.path.join(folder_path, "*.json"))
    
    if not json_files:
        print("Error: No JSON files found. Ensure the files are placed inside the 'nphies_data' folder.")
        return

    code_systems = {}
    
    for file_path in json_files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                if data.get('resourceType') == 'CodeSystem':
                    url = data.get('url', 'Unknown URL')
                    concepts = data.get('concept', [])
                    code_systems[url] = len(concepts)
        except Exception:
            pass

    print("\n--- CodeSystems Found ---")
    for url, count in code_systems.items():
        if count > 0:
            print(f"- {url}: {count} codes")

if __name__ == "__main__":
    analyze_nphies_folder("nphies_data")