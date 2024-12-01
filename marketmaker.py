import threading
from market_maker import market_maker

from flask import Flask
from flask_restful import Api, Resource, reqparse

app = Flask(__name__)
api = Api(app)

class RSI(Resource):
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("value")
        args = parser.parse_args()
        market_maker.rsi = float(args["value"])
        market_maker.logger.info("RSI: {}".format(market_maker.rsi))
        return "RSI: {}".format(args["value"]), 200

class MACD(Resource):
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("value")
        args = parser.parse_args()
        market_maker.macd_histogram = float(args["value"])

        market_maker.logger.info("MACD: {}".format(market_maker.macd_histogram))
        return "MACD: {}".format(args["value"]), 200

class Signal(Resource):
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("type")
        args = parser.parse_args()
       
        if args["type"] == "long":
            market_maker.set_long()

        if args["type"] == "short":
            market_maker.set_short()

        market_maker.logger.info("Signal; received: {}".format(args["type"]))
        return "Signal: {}".format(args["type"]), 200

class Stochastic(Resource):
    
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("strategy")
        args = parser.parse_args()

        if args["strategy"] == "buy":
            market_maker.buy_enable = True
            market_maker.sell_enable = False

        if args["strategy"] == "sell":
            market_maker.sell_enable = True
            market_maker.buy_enable = False

        market_maker.logger.info("Signal received: {}".format(args["strategy"]))
        return "Signal: {}".format(args["strategy"]), 200

# api.add_resource(RSI,"/rsi")
# api.add_resource(MACD,"/macd")
# api.add_resource(Signal,"/signal")
# api.add_resource(Stochastic,"/stoch")

t = threading.Thread(target=app.run, kwargs=dict(host='0.0.0.0', port=80))
t.daemon = True
t.start()

market_maker.run()