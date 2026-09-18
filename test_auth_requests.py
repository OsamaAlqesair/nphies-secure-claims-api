from getpass import getpass

import requests

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 10

# Uses the seeded global approval rule for G43 + 70450.
claim = {
    "diagnosis_code": "G43",
    "service_code": "70450",
    "insurer_id": None,
    "billed_amount": "500.00",
    "allowed_amount": "400.00",
    "copay": "50.00",
    "net_payable": "350.00",
}


def require_status(response: requests.Response, expected: int) -> None:
    if response.status_code != expected:
        # Avoid printing response bodies that could contain credentials/tokens.
        raise SystemExit(
            f"FAIL: expected HTTP {expected}, received {response.status_code}."
        )


def main() -> None:
    with requests.Session() as session:
        # Prevent automatic .netrc authentication during the no-token test.
        session.trust_env = False

        response = session.post(
            f"{BASE_URL}/process-claim",
            json=claim,
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        require_status(response, 401)
        print("PASS: request without a token returned 401.")

        username = input("Username: ").strip()
        password = getpass("Password: ")

        response = session.post(
            f"{BASE_URL}/auth/login",
            json={"username": username, "password": password},
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        require_status(response, 200)
        token = response.json()["access_token"]
        print("PASS: login succeeded.")

        response = session.post(
            f"{BASE_URL}/process-claim",
            json=claim,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        require_status(response, 200)
        if response.json().get("status") != "approved":
            raise SystemExit("FAIL: claim was not approved.")
        print("PASS: authenticated claim returned 200 — approved.")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException:
        raise SystemExit("Connection failed. Check that the API is running.")