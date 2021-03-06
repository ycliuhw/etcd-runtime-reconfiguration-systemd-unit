FROM python:alpine3.6

LABEL maintainer="ycliuhw@gmail.com (Kelvin Liu)"

WORKDIR /opt/app/

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY run.py .
CMD ["python", "./run.py"]
