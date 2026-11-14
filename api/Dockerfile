# api · scheduler 공용 이미지 (시작 명령만 다름). 빌드: `docker build -f api/Dockerfile api` (빌드 컨텍스트 = api/)
#   api        : 기본 CMD (uvicorn, 포트 8000)
#   scheduler  : compose가 `python -m app.scheduler`로 덮어씀
# 라이브러리는 uv.lock 그대로(--locked: pyproject.toml과 어긋나면 빌드가 실패함) 설치. 개발 도구(pytest · ruff)는 안 넣음. 비밀값은 이미지에 안 넣음(실행할 때 env로)

FROM python:3.12-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install --no-cache-dir uv==0.13.8
WORKDIR /srv
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.12-slim
# PYTHONUTF8: 한글 로그가 어떤 로케일에서도 안 깨지게 / PYTHONDONTWRITEBYTECODE: 실행 중 파일을 안 씀(읽기 전용 루트에서도 동작) / PATH: compose 헬스체크가 쓰는 python이 이 환경의 것
ENV PYTHONUTF8=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/opt/venv/bin:$PATH"
COPY --from=build /opt/venv /opt/venv
WORKDIR /srv
COPY app ./app
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app
EXPOSE 8000
# 앞단(nginx)이 127.0.0.1:8000으로만 넘겨 줌(호스트 포트는 compose가 127.0.0.1에만 엶). --no-access-log: 접근 로그에는 쿼리 문자열이 찍히므로 끔
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
