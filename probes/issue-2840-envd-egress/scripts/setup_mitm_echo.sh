#!/usr/bin/env bash
set -euo pipefail

# Provision opensandbox-mitm-ca-v1 Secret and start a TLS echo container on the kind network.
WORK_DIR="${1:-./mitm-ca}"
EGRESS_IMAGE="${EGRESS_IMAGE:-opensandbox/egress:metadata-e2e-f44561a}"
KIND_NODE="${KIND_NODE:-fsb-control-plane}"
NAMESPACE="${NAMESPACE:-opensandbox-dataplane}"
CONTAINER_NAME="${CONTAINER_NAME:-p01-mitm-echo}"

mkdir -p "$WORK_DIR"

cat > "$WORK_DIR/ca.cnf" <<'CNF'
[req]
distinguished_name = dn
prompt = no

[dn]
CN = opensandbox-mitm-ca-v1

[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always, issuer

[v3_echo]
subjectAltName = DNS:echo.test
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid, issuer
CNF

openssl req -x509 -new -newkey rsa:2048 -nodes -days 30 \
  -keyout "$WORK_DIR/tls.key" -out "$WORK_DIR/tls.crt" \
  -config "$WORK_DIR/ca.cnf" -extensions v3_ca 2>/dev/null

CA_FP="$(openssl x509 -in "$WORK_DIR/tls.crt" -noout -fingerprint -sha256 | sed 's/^.*=//; s/://g' | tr 'A-F' 'a-f')"
printf '%s\n' "$CA_FP" > "$WORK_DIR/fingerprint"

openssl req -new -newkey rsa:2048 -nodes \
  -keyout "$WORK_DIR/echo.key" -out "$WORK_DIR/echo.csr" \
  -subj "/CN=echo.test" 2>/dev/null
openssl x509 -req -in "$WORK_DIR/echo.csr" \
  -CA "$WORK_DIR/tls.crt" -CAkey "$WORK_DIR/tls.key" -CAcreateserial \
  -out "$WORK_DIR/echo.crt" -days 30 \
  -extfile "$WORK_DIR/ca.cnf" -extensions v3_echo 2>/dev/null

kubectl -n "$NAMESPACE" create secret generic opensandbox-mitm-ca-v1 \
  --from-file=tls.crt="$WORK_DIR/tls.crt" \
  --from-file=tls.key="$WORK_DIR/tls.key" \
  --from-file=ca.crt="$WORK_DIR/tls.crt" \
  --dry-run=client -o yaml | kubectl apply -f -

cat > "$WORK_DIR/mitm-echo-server.py" <<'PY'
import http.server
import json
import ssl
import sys

port, cert, key = int(sys.argv[1]), sys.argv[2], sys.argv[3]

class Handler(http.server.BaseHTTPRequestHandler):
    def _echo(self):
        body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _echo
    do_POST = _echo

    def log_message(self, *args):
        pass

httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(cert, key)
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
httpd.serve_forever()
PY

KIND_NET="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$KIND_NODE" | awk '{print $1}')"
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER_NAME" --network "$KIND_NET" \
  --entrypoint python3 \
  -v "$(cd "$WORK_DIR" && pwd)/echo.crt":/etc/mitm-echo/tls.crt:ro \
  -v "$(cd "$WORK_DIR" && pwd)/echo.key":/etc/mitm-echo/tls.key:ro \
  -v "$(cd "$WORK_DIR" && pwd)/mitm-echo-server.py":/srv/mitm-echo-server.py:ro \
  "$EGRESS_IMAGE" /srv/mitm-echo-server.py 443 /etc/mitm-echo/tls.crt /etc/mitm-echo/tls.key >/dev/null

sleep 1
ECHO_IP="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$v.IPAddress}}{{end}}' "$CONTAINER_NAME")"
printf '%s\n' "$ECHO_IP" > "$WORK_DIR/echo_ip"
echo "MITM_CA_FINGERPRINT=$CA_FP"
echo "MITM_ECHO_IP=$ECHO_IP (network=$KIND_NET)"
