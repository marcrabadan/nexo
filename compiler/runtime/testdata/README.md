The `online-test-*.txt` files contain a self-signed certificate and its public
fixture private key for the broker TLS rejection test. They protect no real
asset, must never be deployed, and are deliberately named as text fixtures so
Docker can continue excluding all operational `.pem` and `.key` files.
