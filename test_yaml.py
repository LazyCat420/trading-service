import yaml
import urllib.request

url = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/legislators-current.yaml"
response = urllib.request.urlopen(url)
data = yaml.safe_load(response)

print(f"Loaded {len(data)} current legislators")
for i in range(3):
    print(data[i]['name']['first'], data[i]['name']['last'], data[i]['id'].get('bioguide'))
