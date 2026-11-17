"""Production entry point:  python serve.py

Runs the app under waitress (a production WSGI server that works on Windows
and Linux). Each process is one instance; scale out by running several
instances on different ports or hosts behind a load balancer (see
deploy/nginx.conf). They share state through the database.

Environment: HOST (default 127.0.0.1), PORT (8000), THREADS, CONNECTIONS (400).
"""
import os
import socket

from waitress import serve
from waitress.adjustments import Adjustments

import app as application

# Backpressure: a client that reads slowly must slow the response generator
# down, not let the answer pile up in memory. Unread bytes wait in two places,
# and both are capped here. 64 KB is about 200 stream events.
#
# 1. Waitress's own queue. It pauses the generator once more than this many
#    bytes are waiting. Its default is 16 MB, more than any answer here.
OUTPUT_BUFFER_LIMIT = 64 * 1024
# 2. The operating system's send buffer of each connection. Linux grows it to
#    several MB by itself, which would hide a slow client from waitress.
SOCKET_SEND_BUFFER = 64 * 1024

if __name__ == "__main__":
    # Waitress applies these options to every connection it accepts. They are
    # not an argument of serve(), so they are added to its settings class.
    Adjustments.socket_options = [*Adjustments.socket_options,
                                  (socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_SEND_BUFFER)]
    # A stream holds its thread for its whole life, so keep spare threads for
    # ordinary requests on top of the stream cap.
    threads = int(os.environ.get("THREADS", application.MAX_CONCURRENT_STREAMS + 16))
    # Connections this instance holds open at once: open streams, requests
    # waiting for a free thread and idle keep-alive connections from the load
    # balancer. It must be well above the thread count: at the limit waitress
    # stops accepting, and a new request (a health check too) waits unanswered.
    # Waitress's default is 100, less than the streams one instance may hold.
    # 400 stays under what select() handles on Windows (about 500) and under
    # the usual limit of 1024 open files per process on Linux. For more on
    # Linux, raise that limit (ulimit -n) together with CONNECTIONS.
    connections = int(os.environ.get("CONNECTIONS", "400"))
    serve(
        application.app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        threads=threads,
        connection_limit=connections,
        # select() cannot watch more than about 1000 sockets; poll() can (not on
        # Windows). select() measured faster for streams, so it stays the default.
        asyncore_use_poll=connections > 900,
        send_bytes=1,  # flush every SSE event immediately instead of batching
        outbuf_high_watermark=OUTPUT_BUFFER_LIMIT,
    )
