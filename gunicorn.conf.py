# Gunicorn config for 1 vCPU / 2GB RAM droplet

# Use threaded worker to avoid blocking on I/O (e.g. Mistral OCR calls)
worker_class = "gthread"

# 2 workers for a single vCPU — one can handle requests while the other
# is blocked on I/O
workers = 2

# Threads per worker — each thread can handle a request independently
threads = 4

# Total max concurrent requests: 2 workers × 4 threads = 8

# Timeout — receipt OCR can take a while. /upload-receipt-v2 scans inside the
# request: a Mistral call can take its full 55 s, plus a few seconds of image
# pre-processing (verified_consensus_mix) and post-processing. Past this the
# worker is killed, taking its other requests and background scans with it.
timeout = 90

# Bind
bind = "127.0.0.1:8000"
