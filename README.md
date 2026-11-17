# take-home: Visual Assistant API

A small web service: you upload an image, then ask questions about it.
This page has everything needed to take the project over: how to start it, open the test page,
test it, stop it, run it on several servers, and what every setting does.

The task description is in `TASK.md`. This file is the practical guide.

| To do this | Read |
|------------|------|
| Start it and see the test page | Sections 1 and 2 |
| Try every feature by hand | Section 3 |
| Run the automatic tests | Sections 6 and 14 |
| Call the API from your own program | Section 7 |
| Fix a problem | Section 8 |
| Run it for real use, on one server or many | Sections 11 and 12 |
| Measure speed | Section 13 |
| Know what is not done | Section 15 |
| See what to do next | Section 16 |

---

## 1. Start the server

Get the project and open a terminal in its folder:

```
git clone https://github.com/andypc93/take-home.git
cd take-home
```

You need Python 3.8 or newer.

**Windows (PowerShell)**

```
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe app.py
```

**Mac or Linux**

```
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python app.py
```

**With pipenv (the way `TASK.md` describes)**

```
pipenv install
pipenv run python app.py
```

The provided `Pipfile` asks for Python 3.13. With another version, use `pipenv install --python 3.12` (your version).

When you see `Running on http://127.0.0.1:5000`, it is ready.
Nothing else to set up. The database is created by itself the first time.

---

## 2. Open the test page

Go to **http://127.0.0.1:5000/** in your browser. Keep the terminal open: the server runs in it.

This is the Visual Assistant test page. Everything can be tried from it. No other tool is needed.

| Part of the page | What it is for |
|------------------|----------------|
| Top bar | **Reset all data**, **Stop server**, **Dark mode** |
| Image | Drop or choose an image. Four buttons send a bad file on purpose: **GIF named .png**, **Text named .png**, **.exe file**, **Empty file** |
| Conversation | Ask questions about the image. **Stream the answer** shows it word by word. **Cut the connection after 5 events** and **Resume stream** test a broken connection |
| Stored history | What the server has saved for this image. **Refresh** and **Clear** |
| Rate limit | **Send 70 requests** at once, to see the limit work |
| Request counts | A live table of every request the server has answered |
| Last response | The exact answer the server gave to your last action |

The page is part of the service, so it is always at the address of the server:

| Started with | Test page |
|--------------|-----------|
| `app.py` | `http://127.0.0.1:5000/` |
| `app.py` and `PORT=5001` | `http://127.0.0.1:5001/` |
| `serve.py` (section 11) | `http://127.0.0.1:8000/` |
| Several copies behind nginx (section 11) | The address of nginx |

---

## 3. Things to try

Do them in this order. Each one takes a few seconds.

| # | Do this | You should see |
|---|---------|----------------|
| 1 | Drop an image on the box (PNG, JPEG or GIF) | A green "Accepted" line and a short description |
| 2 | Press **Text named .png** | A red "Rejected" line. The server checks the real content |
| 3 | Type a question and press **Send** | The answer appears word by word |
| 4 | Ask a second question | The answer quotes your first question. The history works |
| 5 | Tick **Cut the connection after 5 events**, then ask again | The answer stops half way. "Stored history" does not change |
| 6 | Press **Resume stream** | The answer finishes from where it stopped |
| 7 | Press **Send 70 requests** | Green squares, then red ones. The rate limit works |
| 8 | Open **Request counts** | A live table of every request |
| 9 | Use the **Dark mode** switch | The page changes colour |

Under each answer there is a small line that shows how many tokens were used.

---

## 4. Start again from zero

Press **Reset all data** at the top of the page.

It deletes every image, every conversation and every counter. Use it between test runs.

---

## 5. Stop the server

Press **Stop server** at the top of the page.

Or press `Ctrl + C` in the terminal.

Your data is kept. To start again, repeat the last command of step 1.

---

## 6. Run the automatic tests

**Windows**

```
.\venv\Scripts\python.exe -m pytest tests -q
```

**Mac or Linux**

```
./venv/bin/python -m pytest tests -q
```

It takes less than a minute. All tests should pass.
Some are shown as "skipped". That is normal: they need extra programs (Redis, nginx) that are not required.
Section 14 shows how to run those too.

---

## 7. Use it without the test page

The service is a normal web API. Replace `IMAGE_ID` with the id you get from the first command,
and `picture.png` with an image of yours.

On Windows, run these in **Command Prompt** (cmd), not PowerShell. PowerShell changes the quotes.

```
curl -F "image=@picture.png" http://127.0.0.1:5000/upload
curl -H "Content-Type: application/json" -d "{\"question\":\"What is this?\"}" http://127.0.0.1:5000/chat/IMAGE_ID
curl -N -H "Content-Type: application/json" -d "{\"question\":\"And the size?\"}" http://127.0.0.1:5000/chat-stream/IMAGE_ID
```

| Address | What it does |
|---------|--------------|
| `POST /upload` | Upload an image |
| `POST /chat/IMAGE_ID` | Ask a question, get the whole answer |
| `POST /chat-stream/IMAGE_ID` | Ask a question, get the answer as a stream |
| `GET /chat/IMAGE_ID/history` | See the conversation |
| `DELETE /chat/IMAGE_ID/history` | Clear the conversation |
| `GET /health` | Check the server is alive |
| `GET /metrics` | Request counts |

Errors always come back in the same shape, with a `code` and a `message` that says what to fix.

A stream that breaks can be continued. Every event of a stream has an `id:` line. Send the same
`POST /chat-stream/IMAGE_ID` again with the header `Last-Event-ID:` set to the last id you received, and no body.
The stream goes on from the next event. This works for 5 minutes after the stream started.

When the answer is `429`, the header `Retry-After` says how many seconds to wait.

---

## 8. If something goes wrong

| Problem | Fix |
|---------|-----|
| "Port 5000 is in use" | Use another port. Windows: `$env:PORT=5001; .\venv\Scripts\python.exe app.py`. Mac or Linux: `PORT=5001 ./venv/bin/python app.py`. Then open `http://127.0.0.1:5001/` |
| The page says "429" or "rate limit" | You sent too many requests. Wait one minute, or press **Reset all data** |
| An image is rejected | Only PNG, JPEG and GIF are accepted, up to 16 MB. The red line says the reason |
| `pipenv install` fails | The provided `Pipfile` asks for Python 3.13. Use the steps in section 1 instead, or run `pipenv install --python 3.12` with your version |
| `python` is not found (Windows) | Install Python from python.org and tick "Add Python to PATH" |
| The start command says "No Python at ..." or the `venv` folder does not work | The `venv` folder was made on another machine. Delete it and do section 1 again |
| You want a clean start | Press **Reset all data**, or delete the files `visual_assistant.db*` and the `uploads` folder while the server is stopped |
| The test page does not open | Check that the terminal still shows the server running, and that the port in the address is the port it printed |
| **Reset all data** or **Stop server** says it is turned off | The server was started with `serve.py`. These two buttons only work with `app.py` (see section 10) |
| **Request counts** says "need a token here" | You opened the page from another machine, or `METRICS_TOKEN` is set. The page shows request counts only on the server's own machine and without a token. Everything else on the page still works |
| The page asks for a key | The server was started with `API_KEYS`. Type one of those keys |

---

## 9. What is in this folder

| File or folder | What it is |
|----------------|------------|
| `README.md` | This guide |
| `TASK.md` | The task description |
| `app.py` | The service: all the addresses listed above |
| `database.py` | Tables, database connection, cache |
| `storage.py` | Where image files are kept (a folder, or S3) |
| `static/demo.html` | The test page |
| `tests/` | The automatic tests |
| `loadtest.py` | Sends many requests at once and prints speed and response times (`python loadtest.py --help`) |
| `migrations/`, `alembic.ini` | Database structure changes |
| `serve.py` | Starts the service for real use (instead of `app.py`) |
| `deploy/nginx.conf` | Example setup for running several copies behind a load balancer |
| `requirements.txt`, `Pipfile`, `Pipfile.lock` | The list of packages to install |
| `venv/` | Installed packages (made in step 1; not in the repository) |
| `uploads/`, `visual_assistant.db*` | Your uploaded images and the local database (made at the first start; not in the repository) |
| `.gitignore` | The list of files that stay out of the repository |

---

## 10. Good to know

- The answers are simulated. No real AI service is called, as the task asks. The answers repeat your question so you can see that the history is used.
- **Reset all data** and **Stop server** only work when the server is started with `app.py` on your own machine. They are switched off in real use.
- To require an API key, set `API_KEYS` before starting (for example `API_KEYS=key-one,key-two`). Each call then needs the header `Authorization: Bearer key-one`, each image can only be used with the key that uploaded it, and the test page asks for the key once. Without `API_KEYS` no key is needed.
- For real use, start it with `serve.py` and set `DATABASE_URL` to a PostgreSQL database. Section 11 has the steps.

---

## 11. Run it for real use (one server or many)

`app.py` is for trying the project on your own machine. For real use there are three differences:

| | Trying it (`app.py`) | Real use |
|---|---|---|
| Start command | `python app.py` | `python serve.py` |
| Database | A local file, made by itself | PostgreSQL (`DATABASE_URL`) |
| Copies of the service | One | As many as needed, behind a load balancer |

**One copy**

Mac or Linux:

```
export DATABASE_URL=postgresql+psycopg://user:password@host:5432/visual_assistant
./venv/bin/python serve.py
```

Windows (PowerShell):

```
$env:DATABASE_URL="postgresql+psycopg://user:password@host:5432/visual_assistant"
.\venv\Scripts\python.exe serve.py
```

When you see `Serving on http://127.0.0.1:8000`, it is ready. The test page is at that address.
The tables are created by themselves at the first start.
Without `DATABASE_URL` it uses the local file, which is fine for a quick look but not for several copies.

**Several copies**

Every copy is the same program on a different port or machine. A copy keeps nothing important to itself,
so any copy can answer any request, and a stream that breaks on one copy can be resumed on another.

1. Create or update the tables once: `./venv/bin/python -m alembic upgrade head` (with `DATABASE_URL` set).
2. Start each copy with the same settings and its own port:

```
AUTO_MIGRATE=0 TRUSTED_PROXIES=1 METRICS_TOKEN=choose-a-secret PORT=8001 ./venv/bin/python serve.py
AUTO_MIGRATE=0 TRUSTED_PROXIES=1 METRICS_TOKEN=choose-a-secret PORT=8002 ./venv/bin/python serve.py
```

3. Put nginx in front with `deploy/nginx.conf` (include it inside the `http { }` block of nginx).
   It has two copies listed; add one `server` line for each extra copy.
4. Open the address of nginx. The test page and the API work the same as before.

What the copies must share:

| Thing | How it is shared |
|-------|------------------|
| Image details, conversations, streams that can be resumed | The database. Give every copy the same `DATABASE_URL` |
| Image files | The same folder on every machine (`UPLOAD_FOLDER`), or an S3 bucket (`STORAGE_BACKEND=s3`) |
| Rate limit counters | The database. With `REDIS_URL` set they go to Redis, which is faster |
| Request counts (`/metrics`) | Not shared. Read them from each copy on its own port |

Limits of one copy, and what happens at the limit:

| Limit | Default | At the limit |
|-------|---------|--------------|
| Streams open at once (`MAX_CONCURRENT_STREAMS`) | 100 | The next stream gets `429 too_many_streams` and a `Retry-After` header |
| Requests worked on at once (`THREADS`) | 116 | More requests wait for their turn |
| Connections open at once (`CONNECTIONS`) | 400 | More connections wait until one closes |
| Database connections | Up to 30 | PostgreSQL must allow 30 for each copy (its `max_connections`) |
| Answer waiting for a slow client | About 200 KB for each connection | The answer is produced more slowly, at the speed the client reads. Memory does not grow |

To handle more load, add copies. Section 13 shows the measured effect.

---

## 12. Settings

Every setting has a default that works. Set them as environment variables before the start command.
Mac or Linux: `NAME=value ./venv/bin/python app.py`. Windows (PowerShell): `$env:NAME="value"`, then the start command.

| Setting | Default | What it does |
|---------|---------|--------------|
| `PORT` | 5000 for `app.py`, 8000 for `serve.py` | The port |
| `HOST` | `127.0.0.1` | `serve.py` only. Address to listen on. Use `0.0.0.0` to accept other machines |
| `DATABASE_URL` | A local file | The database, for example `postgresql+psycopg://user:password@host:5432/name` |
| `AUTO_MIGRATE` | `1` | `0`: do not create or update the tables at the start (use with several copies) |
| `UPLOAD_FOLDER` | `uploads` | Folder for the image files |
| `STORAGE_BACKEND` | `local` | `s3` keeps image files in an S3 bucket. Needs `S3_BUCKET` and the package `boto3`. Optional: `S3_PREFIX`, `S3_ENDPOINT_URL` |
| `API_KEYS` | not set | Keys separated by commas. When set, every call needs one (section 10) |
| `UPLOAD_RATE_LIMIT` | `10/60` | Uploads allowed for one client: 10 in 60 seconds |
| `CHAT_RATE_LIMIT` | `60/60` | Questions and history calls allowed for one client: 60 in 60 seconds |
| `REDIS_URL` | not set | Keep the rate limit counters in Redis, for example `redis://127.0.0.1:6379/0` |
| `RATE_LIMIT_STORAGE` | `redis` with `REDIS_URL`, else `database` | Where the counters are kept: `redis`, `database` or `memory` (`memory` is not shared between copies) |
| `MAX_CONCURRENT_STREAMS` | `100` | Streams one copy keeps open at once |
| `THREADS` | streams + 16 | `serve.py` only. Requests one copy works on at once |
| `CONNECTIONS` | `400` | `serve.py` only. Connections one copy keeps open at once |
| `TRUSTED_PROXIES` | `0` | Number of load balancers in front. Set `1` behind nginx, so the rate limit counts the real client |
| `METRICS_TOKEN` | not set | Secret for `/metrics` (`Authorization: Bearer <secret>`). Not set: `/metrics` answers only on its own machine |
| `MOCK_CHAT_DELAY`, `MOCK_VISION_DELAY`, `STREAM_CHUNK_DELAY` | `0.2`, `0.1`, `0.03` | Seconds the simulated AI takes: for an answer, for an image, between two words of a stream |
| `ALLOW_RESET`, `ALLOW_SHUTDOWN` | on with `app.py`, off with `serve.py` | `1` or `0` turns the **Reset all data** and **Stop server** buttons on or off |
| `FLASK_DEBUG` | not set | `1` turns on the debugger of `app.py`. Never use it on a server other people can reach |

---

## 13. Measure speed (load test)

`loadtest.py` sends many requests at once and prints how many were answered per second and how long they took.
The server must be running first.

```
python loadtest.py --url http://127.0.0.1:5000 --scenario chat --requests 200 --concurrency 16
```

The scenarios are `chat`, `upload` and `stream`. `python loadtest.py --help` lists the other options.

With the normal rate limits most answers will be `429`, because one client may send 60 questions a minute.
That is the limit doing its job. To measure speed, start the server with high limits:
`UPLOAD_RATE_LIMIT=1000000/60` and `CHAT_RATE_LIMIT=1000000/60`.

**Measured on 2026-11-16.** One Linux machine with 32 processor cores ran everything: nginx in front of the copies
(`serve.py`), PostgreSQL and Redis, and the load test itself.

More copies do more work. Each copy was held to 16 threads on purpose (`THREADS=16`), with the simulated AI
at its normal 0.2 seconds per answer and 128 clients asking at once:

| Copies | Questions answered per second | Usual wait for an answer |
|--------|-------------------------------|--------------------------|
| 1 | 71 | 1.7 s |
| 2 | 150 | 0.8 s |
| 4 | 279 to 297 | 0.4 s |

Twice the copies answered about twice the questions, with no errors. The copies took equal shares.

The cost of the service alone, with the simulated AI set to take no time (two copies, normal settings):

| Scenario | Clients at once | Per second | Usual time | Slowest 1 in 100 |
|----------|-----------------|------------|------------|------------------|
| Questions | 32 | 386 | 82 ms | 115 ms |
| Uploads | 16 | 556 | 28 ms | 42 ms |
| Streams, from first to last event | 32 | 216 | 147 ms | 187 ms |

Too much load on one copy: 300 clients opened streams at once on a single copy, which holds 100.
573 streams finished, 254 were cut by the client on purpose, 73 got `429 too_many_streams` with `Retry-After`.
No error, nothing left waiting, and the count of open streams went back to 0.

Read these figures with care. Everything shared one machine, so the parts took processor time from each other.
A few runs made while the machine was busy with other work showed a pause of some seconds; eight runs on the quiet machine did not.
The AI is simulated: a real one will be much slower than anything measured here.

---

## 14. Tests that need extra programs

The tests of section 6 run with nothing extra. A few more run only when the program they need is there.
Without it they are shown as "skipped".

| Tests | What they need | How to run them |
|-------|----------------|-----------------|
| `tests/test_redis_real.py` | A Redis server | Set `TEST_REDIS_URL`, for example `TEST_REDIS_URL=redis://127.0.0.1:6379/15`, then run the tests. Use a Redis with nothing important in it: keys named `ratelimit:*` are deleted |
| `tests/test_nginx.py` | nginx installed, on Mac or Linux | Nothing to set. They run by themselves when nginx is found |
| `tests/test_s3_real.py` | The packages `boto3` and `moto[server]` | `pip install boto3 "moto[server]"`, then run the tests. No AWS account is needed |
| All tests, on PostgreSQL | A PostgreSQL database that can be emptied | Set `TEST_DATABASE_URL=postgresql+psycopg://user:password@host:5432/name`. Its tables are emptied before every test, so never use a database with real data |

`tests/test_multiprocess.py` starts real copies of the service by itself (two for most tests) and needs nothing extra.
It checks that copies share conversations, streams and rate limits, that a slow client slows its own answer down,
and that a copy keeps answering with many connections open.

---

## 15. What is not done

- The answers are simulated, as the task asks. With a real AI service, its speed will decide the speed of everything.
- API keys are off unless `API_KEYS` is set. Without them, anyone who knows the id of an image can ask about it. The id is a long random value.
- There are no user accounts. Keys are read at the start; to change them, restart.
- One stream uses one worker thread for as long as it is open. One copy holds 100 streams; for more, add copies.
- When every copy is full, new requests wait. nginx gives up after 60 seconds with a `504` answer. Watch `/metrics` and add copies before that point.
- Only the first bytes and the size in the header of an image are checked. The picture itself is not decoded.
- S3 storage was tested with an S3-compatible test server, not with AWS itself.
- Not run on macOS. The steps for Mac are the same as for Linux, where they were run.
- The speed figures in section 13 come from one machine. On real servers they will differ.

---

## 16. The 10 best next steps

What is missing, most useful first. Section 15 lists the limits; this section says what to do about them.

| # | Next step | Why | Where to start |
|---|-----------|-----|----------------|
| 1 | Connect a real AI service | The answers are simulated. This is the one thing a real user would notice | Replace the bodies of `mock_openai_vision_analysis` and `mock_openai_chat` in `app.py`. They already return the OpenAI format, so the rest of the code stays. Add a time limit, a retry, and a spending limit for each key |
| 2 | Stop using one thread for each stream | A real AI answers for many seconds. One copy holds 100 streams, so slow answers fill it fast | Move the streaming address to an async server (ASGI), or run more copies in the meantime |
| 3 | Answer at once when every copy is full | Today extra requests wait, and nginx gives up after 60 seconds with `504` | In `deploy/nginx.conf`, give each `server` line a `max_conns` value below `CONNECTIONS`, and return a `503` with `Retry-After` in the usual error shape |
| 4 | Real accounts and keys | Keys are a fixed list read at the start, and are off unless `API_KEYS` is set. No sign-up, no way to cancel one key without a restart | Keep keys in the database (only their hash, as `owner` already is), with an address to create and cancel them, and a limit for each key |
| 5 | HTTPS, and access from other websites | The service speaks plain HTTP, and a web page on another address cannot call it (no CORS headers) | Add a certificate in nginx. Add CORS headers for the addresses that may call the API |
| 6 | The missing API addresses | An image cannot be deleted or looked up after the upload; it only expires after 30 days. Stream events have no names, which some clients expect (`created`, `delta`, `completed`) | Add `GET /images/IMAGE_ID` and `DELETE /images/IMAGE_ID`. If named events are wanted, add an `event:` line in `sse_events` in `app.py` |
| 7 | One-command start and automatic checks | Setup is by hand. `requirements.txt` gives only lowest versions, so a new install can get newer packages than were tested. The `Pipfile` asks for Python 3.13 while `TASK.md` says 3.8 or newer; only 3.12 and 3.14 were run | Add a `Dockerfile` and a compose file (service, PostgreSQL, Redis, nginx). Pin exact versions. Run the tests on every change, on each Python version to be supported |
| 8 | Better monitoring | `/metrics` has counts and total time only. There is no way to follow one request through the logs, and nothing warns when things go wrong | Add response-time buckets to `Metrics` in `app.py`, a request id on every log line and in every answer, and alerts on `5xx` answers, `429` answers and `active_streams` |
| 9 | Check images more deeply | Only the first bytes and the size in the header are checked. Hidden data inside a picture (location, camera) is kept. WebP is refused | Open each image with an image library (Pillow) to prove it is whole, save a clean copy without the hidden data, and add WebP |
| 10 | Get ready for large size, and prove it | Old data is cleaned inside normal requests. Each copy has its own cache. The load test ran for seconds on one machine with a small database. macOS and AWS S3 itself were never run | Run `flask --app app cleanup` from a scheduler instead. Add an index on `chat_history.created_at`. Move the cache to Redis. Then run `loadtest.py` for hours, from another machine, against a database with millions of rows |
