FROM python:3.7.4
ADD settings.py /
ADD market_maker /market_maker
RUN pip install bitmex-market-maker
CMD [ "marketmaker" , "XBTUSD"]