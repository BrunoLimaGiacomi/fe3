import subprocess, json, urllib.request, urllib.error

# Get access token
token_result = subprocess.run(
    ["gcloud", "auth", "print-access-token"],
    capture_output=True, text=True, shell=True, timeout=30
)
token = token_result.stdout.strip()
print("Token obtained:", bool(token))

# Build the filter
filt = (
    'protoPayload.methodName="SetIamPolicy" '
    'AND protoPayload.authenticationInfo.principalEmail="bgiacomi2@uolinc.com" '
    'AND timestamp>="2026-08-31T00:00:00Z" '
    'AND timestamp<="2026-08-31T23:59:59Z"'
)

# Call the Logging REST API
payload = json.dumps({
    "filter": filt,
    "pageSize": 50,
    "resourceNames": ["projects/uolcs-caribe-qa"]
}).encode("utf-8")

req = urllib.request.Request(
    "https://logging.googleapis.com/v2/entries:list",
    data=payload,
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    },
    method="POST"
)

try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        entries = data.get("entries", [])
        print(f"Found {len(entries)} entries\n")
        for i, entry in enumerate(entries):
            ts = entry.get("timestamp", "")
            proto = entry.get("protoPayload", {})
            method = proto.get("methodName", "")
            auth = proto.get("authenticationInfo", {}).get("principalEmail", "")
            req_data = proto.get("request", {})
            policy_delta = proto.get("serviceData", {}).get("policyDelta", {})
            binding_deltas = policy_delta.get("bindingDeltas", [])
            print(f"--- Entry {i+1} ---")
            print(f"  Timestamp: {ts}")
            print(f"  Method: {method}")
            print(f"  Principal: {auth}")
            if binding_deltas:
                for bd in binding_deltas:
                    action = bd.get("action", "")
                    role = bd.get("role", "")
                    member = bd.get("member", "")
                    print(f"  Delta: action={action} role={role} member={member}")
            else:
                print("  No bindingDeltas found in policyDelta")
                # Try alternative locations
                req_policy = req_data.get("policy", {})
                if req_policy:
                    print(f"  Request policy bindings count: {len(req_policy.get('bindings', []))}")
                print(f"  Request keys: {list(req_data.keys())}")
                sd = proto.get("serviceData", {})
                print(f"  serviceData keys: {list(sd.keys())}")
            print()
except urllib.error.HTTPError as e:
    print(f"HTTP Error {e.code}: {e.read().decode('utf-8')[:5000]}")
except Exception as e:
    print(f"Error: {e}")