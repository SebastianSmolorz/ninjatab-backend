# Gunicorn config for 1 vCPU / 2GB RAM droplet

# Use threaded worker to avoid blocking on I/O (e.g. Mistral OCR calls)
worker_class = "gthread"

# 2 workers for a single vCPU — one can handle requests while the other
# is blocked on I/O
workers = 2

# Threads per worker — each thread can handle a request independently
threads = 4

# Total max concurrent requests: 2 workers × 4 threads = 8

# Timeout — receipt OCR can take a while
timeout = 60

# Bind
bind = "127.0.0.1:8000"
