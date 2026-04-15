FROM python:3.12-slim

# Keep the container output clean and avoid writing .pyc files we do not need.
ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	PIP_DISABLE_PIP_VERSION_CHECK=1

# Everything runs from /app.
WORKDIR /app

# Use an unprivileged runtime user instead of root.
RUN adduser --disabled-password --gecos "" appuser

# Install Python dependencies before copying app code to keep rebuilds fast.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --no-compile --requirement requirements.txt

# Copy the server code and hand ownership to the runtime user.
COPY --chown=appuser:appuser pdf_server.py ./

# Drop privileges for normal execution.
USER appuser

# Start the MCP server.
CMD ["python", "pdf_server.py"]