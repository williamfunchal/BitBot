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
        market_maker.trand_type = args["type"]

        market_maker.logger.info("Signal; received: {}".format(market_maker.trand_type))
        return "Signal: {}".format(args["type"]), 200

api.add_resource(RSI,"/rsi")
api.add_resource(MACD,"/macd")
api.add_resource(Signal,"/signal")

t = threading.Thread(target=app.run, kwargs=dict(host='0.0.0.0', port=80))
t.daemon = True
t.start()

market_maker.run()