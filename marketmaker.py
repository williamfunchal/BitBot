import threading
from market_maker import market_maker

from flask import Flask
from flask_restful import Api, Resource, reqparse

app = Flask(__name__)
api = Api(app)



class Alert(Resource):
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("value")
        args = parser.parse_args()
        market_maker.rsi = float(args["value"])
        market_maker.logger.info("RSI: {}".format(market_maker.rsi))
        return "RSI: {}".format(args["value"]), 200

api.add_resource(Alert,"/alerts")

t = threading.Thread(target=app.run)
t.daemon = True
t.start()

market_maker.run()