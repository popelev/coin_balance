FROM python:3.9-slim-buster
COPY . .
WORKDIR .
RUN python3 -m pip install -r requirements.txt
EXPOSE 8000
CMD gunicorn main:app --workers 4 --worker-class uvicorn.workers.UvicornWorker --bind=0.0.0.0:8000 --log-level debug --timeout 120 
