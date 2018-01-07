FROM python:alpine3.6

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY run.py .
CMD ["python", "./run.py"]
