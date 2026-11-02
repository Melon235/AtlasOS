# Atlas UAT sandbox image: pinned Python + Git (from the base image) + pytest for the fixture.
# Build locally, then select it in experiments/configs/v2-uat.yaml:
#   docker build -t atlas-uat-python:3.12 experiments/fixtures/uat_image
FROM python:3.12
RUN pip install --no-cache-dir \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    pytest==9.1.1
