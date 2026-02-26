import csv
import subprocess
import json
import time
import urllib.parse

city_pairs_file = 'country_city_pairs.csv'
json_file_path = 'speedtest_servers.json'
csv_file_path = 'speedtest_servers.csv'


def read_csv_to_set(csv_file_path):
    country_city_set = set()

    with open(csv_file_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.reader(csvfile)
        next(reader)  # Skip the header
        for row in reader:
            country_city_set.add(tuple(row))

    return country_city_set


def fetch_url(url, retries=3):
    """Fetch a URL using curl to avoid Python SSL/TLS fingerprinting issues."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    'curl', '-s', '--max-time', '30',
                    '-H', 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/120.0.0.0 Safari/537.36',
                    '-H', 'Accept: application/json, text/plain, */*',
                    '-H', 'Accept-Language: en-US,en;q=0.9',
                    '-H', 'Referer: https://www.speedtest.net/',
                    '-w', '\n%{http_code}',
                    url,
                ],
                capture_output=True, text=True, timeout=35,
            )
            # Last line is HTTP status code
            lines = result.stdout.rsplit('\n', 1)
            body = lines[0] if len(lines) > 1 else ''
            status = int(lines[-1]) if lines[-1].isdigit() else 0
            return status, body
        except Exception as e:
            print(f"  Attempt {attempt + 1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(5)
    return 0, ''


def call_speedtest_api_for_pairs(pairs):
    filtered_servers = []
    seen_server_ids = set()

    for country, city in pairs:
        print(f"Fetching data for {country}, {city}")

        encoded_city = urllib.parse.quote(city)
        url = f"https://www.speedtest.net/api/js/servers?engine=js&https_functional=true&limit=100&search={encoded_city}"

        status, body = fetch_url(url)

        if status == 200 and body:
            try:
                servers = json.loads(body)
            except json.JSONDecodeError as e:
                print(f"  JSON decode error for {country}, {city}: {e}")
                continue

            for server in servers:
                server_id = server.get('id')
                if server_id and server_id not in seen_server_ids:
                    seen_server_ids.add(server_id)
                    filtered_servers.append(server)
        else:
            print(f"  Failed to fetch data for {country}, {city} (HTTP {status})")

        time.sleep(5)

    return filtered_servers

def json_to_csv(servers, csv_file_path):
    # Open a CSV file for writing
    with open(csv_file_path, 'w', newline='', encoding='utf-8') as csv_file:
        # Create a CSV writer object
        csv_writer = csv.writer(csv_file)
        header_written = False

        for server in servers:
            if not header_written:
                # Write the header
                header = server.keys()
                csv_writer.writerow(header)
                header_written = True
            # Write the data rows
            csv_writer.writerow(server.values())



# Replace 'sorted_country_city_pairs.csv' with your actual CSV file path
country_city_pairs = read_csv_to_set(city_pairs_file)

# Call the Speedtest API and store the results
speedtest_results = call_speedtest_api_for_pairs(country_city_pairs)

# Saving the results to a JSON file with UTF-8 encoding
with open(json_file_path, 'w', encoding='utf-8') as f:
    json.dump(speedtest_results, f, ensure_ascii=False, indent=4)

# Convert the results to CSV and save it
json_to_csv(speedtest_results, csv_file_path)