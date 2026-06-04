# slim variant keeps the image small — no build tools, no extras
FROM python:3.11-slim

# set working directory inside the container
WORKDIR /app

# --- dependency layer (cached separately for faster rebuilds) ---
# copy requirements first so Docker reuses this layer if source code changes but deps don't
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- application source ---
# copy the rest of the repo after deps are installed
COPY . .

# --- non-root user (security) ---
# UID/GID 999 matches the host `savant` user so bind-mounted volumes
# (/app/memory, /app/logs) are writable without chmod hacks on the host.
RUN groupadd --gid 999 savant && \
    useradd --uid 999 --gid 999 --no-create-home --shell /bin/false savant

# transfer ownership of /app so the non-root user can read/write (logs, memory mounts)
RUN chown -R savant:savant /app

# switch to the non-root user for all subsequent commands and at runtime
USER savant

# document which port the app listens on (does not publish it — docker-compose handles that)
EXPOSE 8000

# start the FastAPI app via uvicorn; 0.0.0.0 makes it reachable from outside the container
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
