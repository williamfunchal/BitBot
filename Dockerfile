FROM python:3.7.4

RUN pip install bitmex-market-maker
RUN pip install flask-restful

WORKDIR /src

COPY settings.py /src/
COPY marketmaker.py /src/
COPY market_maker/* /src/market_maker/
COPY market_maker/auth/* /src/market_maker/auth/
COPY market_maker/utils/* /src/market_maker/utils/
COPY market_maker/ws/* /src/market_maker/ws/

CMD [ "python" , "marketmaker.py"]