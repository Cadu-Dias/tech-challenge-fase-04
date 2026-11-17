FROM python:3.11-alpine
WORKDIR /app
COPY responder.py .
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["python", "-u", "responder.py"]
