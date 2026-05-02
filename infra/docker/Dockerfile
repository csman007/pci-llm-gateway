FROM public.ecr.aws/lambda/python:3.12

WORKDIR /var/task

COPY services/ ./services/
COPY services/api-gateway/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONPATH=/var/task/services/api-gateway:/var/task/services

CMD ["main.handler"]
