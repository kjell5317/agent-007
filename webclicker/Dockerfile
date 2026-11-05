FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install "playwright>=1.51,<2" \
    && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*
COPY clicker.py /clicker.py
CMD ["python", "/clicker.py"]
