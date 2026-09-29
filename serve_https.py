"""Serve this folder over HTTPS so a phone can open x30_provisioner.html with Web Bluetooth.

    python serve_https.py                      # uses cert.pem / key.pem in this folder
    python serve_https.py --cert c.pem --key k.pem --port 8443

Make the certificate with mkcert (trusted, no warnings once its root CA is installed on the phone):

    winget install FiloSottile.mkcert
    mkcert -install
    mkcert -cert-file cert.pem -key-file key.pem <pc-ip> localhost
    mkcert -CAROOT                             # copy rootCA.pem from there to the phone and install it

or, with a browser warning on every device, a plain self-signed one:

    openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=x30" -keyout key.pem -out cert.pem
"""
import argparse
import os
import socket
import ssl
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--cert', default='cert.pem')
    parser.add_argument('--key', default='key.pem')
    parser.add_argument('--port', type=int, default=8443)
    args = parser.parse_args()
    for path in (args.cert, args.key):
        if not os.path.exists(path):
            sys.exit('%s not found; see the notes at the top of this file' % path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(args.cert, args.key)
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print('Serving on https://%s:%d/x30_provisioner.html  (Ctrl-C to stop)' % (lan_ip(), args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
