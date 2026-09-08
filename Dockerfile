# Builds and runs with no network access at runtime.
# Only the pip install needs a network; the extraction itself never opens a socket
# (proven by tests/test_constraints.py::test_pipeline_opens_no_socket).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

# PYTHONHASHSEED and the thread caps are determinism controls, not tuning:
# unseeded string hashing and multi-threaded BLAS reductions are both sources of
# run-to-run variation, and the brief diffs two runs byte-for-byte.

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

COPY config.yaml ./
COPY tests/ ./tests/
COPY pytest.ini ./

# Fail the build rather than ship a broken image.
RUN pip install --no-cache-dir pytest && python -m pytest -q

ENTRYPOINT ["python", "-m", "zextract"]
CMD ["--help"]
