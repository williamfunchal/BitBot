from __future__ import absolute_import
from time import sleep
import sys
from datetime import datetime
from os.path import getmtime
import random
import requests
import atexit
import signal

from market_maker import bitmex
from market_maker.settings import settings
from market_maker.utils import log, constants, errors, math

# Used for reloading the bot - saves modified times of key files
import os
watched_files_mtimes = [(f, getmtime(f)) for f in settings.WATCHED_FILES]


#
# Helpers
#
logger = log.setup_custom_logger('root')
rsi = 50
macd_histogram = 0
short_enable = False
long_enable = False
buy_enable = False
sell_enable = False
trand_type = '' 


class ExchangeInterface:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        if len(sys.argv) > 1:
            self.symbol = sys.argv[1]
        else:
            self.symbol = settings.SYMBOL
        self.bitmex = bitmex.BitMEX(base_url=settings.BASE_URL, base_ws_url=settings.BASE_WS_URL, symbol=self.symbol,
                                    apiKey=settings.API_KEY, apiSecret=settings.API_SECRET,
                                    orderIDPrefix=settings.ORDERID_PREFIX, postOnly=settings.POST_ONLY,
                                    timeout=settings.TIMEOUT)

        self.leverage = settings.LEVERAGE

    def cancel_order(self, order):
        tickLog = self.get_instrument()['tickLog']
        logger.info("Canceling: %s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))
        while True:
            try:
                self.bitmex.cancel(order['orderID'])
                sleep(settings.API_REST_INTERVAL)
            except ValueError as e:
                logger.info(e)
                sleep(settings.API_ERROR_INTERVAL)
            else:
                break

    def cancel_all_orders(self):
        if self.dry_run:
            return

        logger.info("Resetting current position. Canceling all existing orders.")
        tickLog = self.get_instrument()['tickLog']

        # In certain cases, a WS update might not make it through before we call this.
        # For that reason, we grab via HTTP to ensure we grab them all.
        orders = self.bitmex.http_open_orders()

        for order in orders:
            logger.info("Canceling: %s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))

        if len(orders):
            self.bitmex.cancel([order['orderID'] for order in orders])

        sleep(settings.API_REST_INTERVAL)

    def get_portfolio(self):
        contracts = settings.CONTRACTS
        portfolio = {}
        for symbol in contracts:
            position = self.bitmex.position(symbol=symbol)
            instrument = self.bitmex.instrument(symbol=symbol)

            if instrument['isQuanto']:
                future_type = "Quanto"
            elif instrument['isInverse']:
                future_type = "Inverse"
            elif not instrument['isQuanto'] and not instrument['isInverse']:
                future_type = "Linear"
            else:
                raise NotImplementedError("Unknown future type; not quanto or inverse: %s" % instrument['symbol'])

            if instrument['underlyingToSettleMultiplier'] is None:
                multiplier = float(instrument['multiplier']) / float(instrument['quoteToSettleMultiplier'])
            else:
                multiplier = float(instrument['multiplier']) / float(instrument['underlyingToSettleMultiplier'])

            portfolio[symbol] = {
                "currentQty": float(position['currentQty']),
                "futureType": future_type,
                "multiplier": multiplier,
                "markPrice": float(instrument['markPrice']),
                "spot": float(instrument['indicativeSettlePrice'])
            }

        return portfolio

    def calc_delta(self):
        """Calculate currency delta for portfolio"""
        portfolio = self.get_portfolio()
        spot_delta = 0
        mark_delta = 0
        for symbol in portfolio:
            item = portfolio[symbol]
            if item['futureType'] == "Quanto":
                spot_delta += item['currentQty'] * item['multiplier'] * item['spot']
                mark_delta += item['currentQty'] * item['multiplier'] * item['markPrice']
            elif item['futureType'] == "Inverse":
                spot_delta += (item['multiplier'] / item['spot']) * item['currentQty']
                mark_delta += (item['multiplier'] / item['markPrice']) * item['currentQty']
            elif item['futureType'] == "Linear":
                spot_delta += item['multiplier'] * item['currentQty']
                mark_delta += item['multiplier'] * item['currentQty']
        basis_delta = mark_delta - spot_delta
        delta = {
            "spot": spot_delta,
            "mark_price": mark_delta,
            "basis": basis_delta
        }
        return delta

    def get_delta(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.get_position(symbol)['currentQty']

    def get_instrument(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.instrument(symbol)

    def get_margin(self):
        if self.dry_run:
            return {'marginBalance': float(settings.DRY_BTC), 'availableFunds': float(settings.DRY_BTC)}
        return self.bitmex.funds()

    def get_orders(self):
        if self.dry_run:
            return []
        return self.bitmex.open_orders()

    def get_highest_buy(self):
        buys = [o for o in self.get_orders() if o['side'] == 'Buy']
        if not len(buys):
            return {'price': -2**32}
        highest_buy = max(buys or [], key=lambda o: o['price'])
        return highest_buy if highest_buy else {'price': -2**32}

    def get_lowest_sell(self):
        sells = [o for o in self.get_orders() if o['side'] == 'Sell']
        if not len(sells):
            return {'price': 2**32}
        lowest_sell = min(sells or [], key=lambda o: o['price'])
        return lowest_sell if lowest_sell else {'price': 2**32}  # ought to be enough for anyone

    def get_position(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.position(symbol)

    def close_position(self, quantity, symbol=None):
        if self.dry_run:
            return
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.close_position(quantity)

    def stop_limit(self, quantity, price, trigger_price):        
        return self.bitmex.place_stop_limit(quantity, price, trigger_price)

    def get_ticker(self, symbol=None):
        if symbol is None:
            symbol = self.symbol
        return self.bitmex.ticker_data(symbol)

    def is_open(self):
        """Check that websockets are still open."""
        return not self.bitmex.ws.exited

    def check_market_open(self):
        instrument = self.get_instrument()
        if instrument["state"] != "Open" and instrument["state"] != "Closed":
            raise errors.MarketClosedError("The instrument %s is not open. State: %s" %
                                           (self.symbol, instrument["state"]))

    def check_if_orderbook_empty(self):
        """This function checks whether the order book is empty"""
        instrument = self.get_instrument()
        if instrument['midPrice'] is None:
            raise errors.MarketEmptyError("Orderbook is empty, cannot quote")

    def amend_orders(self, orders,liquidation_price=None, position_price=None):
        if self.dry_run:
            return orders
        #check if position_price is not None
        if position_price is not None:
            
            orders_to_remove = []
            #loop orders
            for id, order in enumerate(orders):
                #check if position_price is positive            
                if position_price is not None and position_price > 0:
                    #check if the order price is lower than the liquidation price. If True remove the order from orders.
                    if order['price'] < liquidation_price:
                        orders_to_remove.append(order)
                        continue
                    #else, continue to the next order
                    continue

                #check if position_price is negative
                if position_price is not None and position_price < 0:
                    #check if the order price is greater than the liquidation price. If True remove the order from orders.
                    if order['price'] > liquidation_price:
                        orders_to_remove.append(order)
                        continue
                    #else, continue to the next order
                    continue

            #check if orders_to_remove is not empty
            #loop orders_to_remove and remove the order from orders.
            if orders_to_remove is not None:
                for id, order_to_remove in enumerate(orders_to_remove):
                    orders.remove(order_to_remove)
                    
                     
        return self.bitmex.amend_orders(orders)

    def create_orders(self, orders, liquidation_price=None, position_price=None):
        
        #check if position_price is not None
        if position_price is not None:
            orders_to_remove = []
            #loop orders
            for id, order in enumerate(orders):
                #check if position_price is positive            
                if position_price is not None and position_price > 0:
                    #check if the order price is lower than the liquidation price. If True remove the order from orders.
                    if order['price'] < liquidation_price:
                        orders_to_remove.append(id)
                        continue
                    #else, continue to the next order
                    continue

                #check if position_price is negative
                if position_price is not None and position_price < 0:
                    #check if the order price is greater than the liquidation price. If True remove the order from orders.
                    if order['price'] > liquidation_price:
                        orders_to_remove.append(id)
                        continue
                    #else, continue to the next order
                    continue

            #check if orders_to_remove is not empty
            #loop orders_to_remove and remove the order from orders.
            if orders_to_remove is not None:
                for id, order_to_remove in enumerate(orders_to_remove):
                    orders.remove(order_to_remove)
        
        if self.dry_run:
            return orders
        
        return self.bitmex.create_orders(orders)

    def place_order(self,quantity,price):
        if self.dry_run:
            return
        return self.bitmex.place_order(quantity,price)

    def cancel_orders(self, orders):
        if self.dry_run:
            return orders
        return self.bitmex.cancel([order['orderID'] for order in orders])

    def isolate_margin(self, symbol, leverage, rethrow_errors):
        if self.dry_run:
            return

        if leverage > self.leverage:
            leverage = self.leverage

        return self.bitmex.isolate_margin(symbol, leverage, rethrow_errors)


class OrderManager:

    

    def __init__(self):
        self.exchange = ExchangeInterface(settings.DRY_RUN)
        self.leverage = settings.LEVERAGE
        self.max_profit = settings.TARGET_TO_PROFIT
        self.take_profit_trigger = settings.TAKE_PROFIT_TRIGGER
        self.trailling = False
        self.auto_deleverage = False
        self.stop_placed = False
        self.position_start_entry_qty = float(settings.POSITION_START_ENTRY_QTY)
        # Once exchange is created, register exit handler that will always cancel orders
        # on any error.
        atexit.register(self.exit)
        signal.signal(signal.SIGTERM, self.exit)

        logger.info("Using symbol %s." % self.exchange.symbol)

        if settings.DRY_RUN:
            logger.info("Initializing dry run. Orders printed below represent what would be posted to BitMEX.")
        else:
            logger.info("Order Manager initializing, connecting to BitMEX. Live run: executing real trades.")

        self.start_time = datetime.now()
        self.instrument = self.exchange.get_instrument()
        self.starting_qty = self.exchange.get_delta()
        self.running_qty = self.starting_qty
        self.reset()

    def reset(self):
        self.exchange.cancel_all_orders()
        self.sanity_check()
        self.print_status()

        # Create orders and converge.
        self.place_orders()

    def print_status(self):
        """Print the current MM status."""

        margin = self.exchange.get_margin()
        position = self.exchange.get_position()
        self.running_qty = self.exchange.get_delta()
        tickLog = self.exchange.get_instrument()['tickLog']
        self.start_XBt = margin["marginBalance"]

        logger.info("Current XBT Balance: %.6f" % XBt_to_XBT(self.start_XBt))
        logger.info("Current Contract Position: %d" % self.running_qty)
        if settings.CHECK_POSITION_LIMITS:
            logger.info("Position limits: %d/%d" % (settings.MIN_POSITION, settings.MAX_POSITION))
        if position['currentQty'] != 0:
            logger.info("Avg Cost Price: %.*f" % (tickLog, float(position['avgCostPrice'])))
            logger.info("Avg Entry Price: %.*f" % (tickLog, float(position['avgEntryPrice'])))
        logger.info("Contracts Traded This Run: %d" % (self.running_qty - self.starting_qty))
        logger.info("Total Contract Delta: %.4f XBT" % self.exchange.calc_delta()['spot'])        
        
    def initialize_position(self):
        global macd_histogram
        global rsi
        global long_enable
        global short_enable
        global sell_enable
        global buy_enable
        global trand_type

        ticker = ticker = self.exchange.get_ticker()
        position = self.exchange.get_position()
        position_start_entry_qty = self.position_start_entry_qty
        
        qty = position['currentQty']
        if "unrealisedRoePcnt" in position:
            roe = position['unrealisedRoePcnt']

        if qty == 0: 
            self.stop_placed = False

        if macd_histogram > 0 and rsi < 50:
            long_enable = True
            short_enable = False
            macd_histogram = 0
            return

        if macd_histogram < 0 and rsi > 50:
            long_enable = False
            short_enable = True
            macd_histogram = 0
            return

        if long_enable == True and buy_enable == True:
            # Stop 
            if qty < 0 and roe < 0:
                self.exchange.place_order(float(qty) * -1, ticker['buy'])
            ###
            if qty == 0 or (qty < (position_start_entry_qty / 2) and qty > 0):
                self.exchange.place_order(position_start_entry_qty, ticker['buy'])

            return

        elif short_enable == True and sell_enable == True:
            #Stop
            if qty > 0 and roe < 0:
                self.exchange.place_order(float(qty) * -1, ticker['sell'])
            ###
            if qty == 0 or (qty > ((position_start_entry_qty * -1) / 2) and qty < 0):
                position_start_entry_qty *= -1
                self.exchange.place_order(position_start_entry_qty, ticker['sell'])

            return

        """ elif short_enable == True and buy_enable == True:
            #Stop
            if qty > 0 and roe < 0:
                self.exchange.place_order(float(qty) * -1, ticker['sell'])
            return

        elif long_enable == True and sell_enable == True:
            # Stop 
            if qty < 0 and roe < 0:
                self.exchange.place_order(float(qty) * -1, ticker['buy'])
            return """


        """ elif (long_enable == False and buy_enable == True) or (short_enable == False and sell_enable == True):
            if qty == 0:
                if buy_enable:
                    self.exchange.place_order(100, ticker['buy'])
                    return

                if sell_enable:
                    self.exchange.place_order(-100, ticker['sell'])
                    return """
        

    def verify_profit(self):
        global long_enable
        global short_enable

        """Verify profit and Close Position at market Price"""        

        position = self.exchange.get_position()
        ticker = ticker = self.exchange.get_ticker()
        tickLog = self.exchange.get_instrument()['tickLog']
        entry_price = position["avgEntryPrice"]
        if "unrealisedRoePcnt" in position:
            roe = position['unrealisedRoePcnt']

        if "unrealisedPnlPcnt" in position:
            pnl_percent = position['unrealisedPnlPcnt']

        if "unrealisedGrossPnl"in position:
            pnl = position['unrealisedGrossPnl']

        qty = position['currentQty']

        is_buy_position = False
        is_sell_position = False       
        

        logger.info("Target ROE: %.*f" % (5, float(self.max_profit)))

        if qty != 0:        
            if qty < 0: 
                is_sell_position = True           
            
            #if (is_sell_position == True and (self.take_profit_trigger * -1) > qty ) or (is_sell_position == False and self.take_profit_trigger < qty ):
            if  self.trailling == False and self.max_profit < roe:     
                self.trailling = True
                self.max_profit = roe
                return True

            if self.trailling == True and self.max_profit < roe :
                self.max_profit = roe            
                return True

            logger.info('unrealisedPnlPcnt: '+str(position['unrealisedPnlPcnt'])+' - unrealisedRoePcnt: '+str(position['unrealisedRoePcnt']))
            logger.info('SUM ROE: '+str(position['unrealisedRoePcnt'] + position['unrealisedPnlPcnt']))

            if self.trailling == True and (self.max_profit - (self.max_profit * 0.1)) >= roe :    
                self.exchange.cancel_all_orders()        
                logger.info("Aproximated realized (Market Price) PNL: %.*f" % (3, float(pnl))) 
                self.exchange.close_position(float(qty) * -1)
                """ stop_qty = float(qty) * -1                
                if stop_qty > 0 : stop_ticker = ticker['buy']
                if stop_qty < 0 : stop_ticker = ticker['sell']
                self.exchange.place_order(stop_qty, stop_ticker) """
                #self.exchange.stop_limit(stop_qty,stop_ticker,stop_ticker) """
                logger.info("ROE realized: %.*f" % (3, float(roe)))
                self.trailling = False
                self.max_profit = float(settings.TARGET_TO_PROFIT)

                ## Wait for the next Signal
                #long_enable = False
                #short_enable = False
                return True

            #This uses ProfitLimit 
            """ if (is_sell_position == True and qty <= settings.MIN_POSITION) or (is_sell_position == False and qty >= settings.MAX_POSITION):
                if self.stop_placed == False:
                    stop_qty = round((float(qty) * -1) / 3 , 0)        
                            
                    if stop_qty > 0 : 
                        exec_price =  entry_price - 1
                    if stop_qty < 0 : 
                        exec_price = entry_price + 1
                    self.exchange.stop_limit(stop_qty,exec_price,entry_price)
                    logger.info("Creating stop at: %.*f" % (2, float(exec_price))) 
                    self.stop_placed = True
                    self.max_profit = float(settings.TARGET_TO_PROFIT)
                    return True """

            """ if self.stop_placed == True:
                stop_qty = round((float(qty) * -1) / 3 , 0) 
                if stop_qty > 0 : 
                    if ticker['buy'] < entry_price:
                        self.trailling = True
                        self.max_profit = roe
                if stop_qty < 0 : 
                    if ticker['sell'] > entry_price:
                        self.trailling = True
                        self.max_profit = roe
                self.stop_placed = False
                return True
                
            if ((is_sell_position == True and qty <= settings.MIN_POSITION) or (is_sell_position == False and qty >= settings.MAX_POSITION)) and self.trailling == False and roe < -0.1:
                if self.stop_placed == False:
                    self.stop_placed = True
                    return True """

            if self.trailling:
                logger.info("Trailling: %.*f" % (tickLog, float(self.max_profit)))

            logger.info("Unrealised PNL: %.*f" % (2, float(pnl)))
            logger.info("Unrealized ROE: %.*f" % (5, roe))
            logger.info("Unrealized PNL percent: %.*f" % (5, float(pnl_percent)))

    def verify_leverage(self):
        position = self.exchange.get_position()
        qty = position['currentQty']

        if qty == 0:
            if "leverage" in position and position['leverage'] != self.leverage:
                self.exchange.isolate_margin(self.exchange.symbol,self.leverage,True)


    def get_ticker(self):
        ticker = self.exchange.get_ticker()
        tickLog = self.exchange.get_instrument()['tickLog']
        position = self.exchange.get_position()


        # Set up our buy & sell positions as the smallest possible unit above and below the current spread
        # and we'll work out from there. That way we always have the best price but we don't kill wide
        # and potentially profitable spreads.
        """ try:
            if settings.MAINTAIN_ENTRY_PRICE_SPREAD_CENTER == True:
                self.start_position_buy = float(position["avgEntryPrice"]) + float(self.instrument['tickSize'])
                self.start_position_sell = float(position["avgEntryPrice"]) - float(self.instrument['tickSize'])
        except Exception as position_exc:
            logger.info("no positions Yet! Spread will start on start_position by market value!")
            self.start_position_buy = ticker["buy"] + self.instrument['tickSize']
            self.start_position_sell = ticker["sell"] - self.instrument['tickSize']
        else:
            if settings.MAINTAIN_ENTRY_PRICE_SPREAD_CENTER == False:
                self.start_position_buy = ticker["buy"] + self.instrument['tickSize']
                self.start_position_sell = ticker["sell"] - self.instrument['tickSize'] """

        self.start_position_buy = ticker["buy"] + self.instrument['tickSize']
        self.start_position_sell = ticker["sell"] - self.instrument['tickSize']

        # If we're maintaining spreads and we already have orders in place,
        # make sure they're not ours. If they are, we need to adjust, otherwise we'll
        # just work the orders inward until they collide.
        if settings.MAINTAIN_SPREADS:
            if ticker['buy'] == self.exchange.get_highest_buy()['price']:
                self.start_position_buy = ticker["buy"]
            if ticker['sell'] == self.exchange.get_lowest_sell()['price']:
                self.start_position_sell = ticker["sell"]
            

        # Back off if our spread is too small.
        if self.start_position_buy * (1.00 + settings.MIN_SPREAD) > self.start_position_sell:
            self.start_position_buy *= (1.00 - (settings.MIN_SPREAD / 2))
            self.start_position_sell *= (1.00 + (settings.MIN_SPREAD / 2))

        # Midpoint, used for simpler order placement.
        self.start_position_mid = ticker["mid"]
        logger.info(
            "%s Ticker: Buy: %.*f, Sell: %.*f" %
            (self.instrument['symbol'], tickLog, ticker["buy"], tickLog, ticker["sell"])
        )
        logger.info('Start Positions: Buy: %.*f, Sell: %.*f, Mid: %.*f' %
                    (tickLog, self.start_position_buy, tickLog, self.start_position_sell,
                     tickLog, self.start_position_mid))
        return ticker

    def get_price_offset(self, index):
        """Given an index (1, -1, 2, -2, etc.) return the price for that side of the book.
           Negative is a buy, positive is a sell."""
        # Maintain existing spreads for max profit
        if settings.MAINTAIN_SPREADS:
            start_position = self.start_position_buy if index < 0 else self.start_position_sell
            # First positions (index 1, -1) should start right at start_position, others should branch from there
            index = index + 1 if index < 0 else index - 1
        else:
            # Offset mode: ticker comes from a reference exchange and we define an offset.
            start_position = self.start_position_buy if index < 0 else self.start_position_sell

            # If we're attempting to sell, but our sell price is actually lower than the buy,
            # move over to the sell side.
            if index > 0 and start_position < self.start_position_buy:
                start_position = self.start_position_sell
            # Same for buys.
            if index < 0 and start_position > self.start_position_sell:
                start_position = self.start_position_buy

        return math.toNearest(start_position * (1 + settings.INTERVAL) ** index, self.instrument['tickSize'])

    ###
    # Orders
    ###

    def place_orders(self):
        """Create order items for use in convergence."""

        buy_orders = []
        sell_orders = []
        # Create orders from the outside in. This is intentional - let's say the inner order gets taken;
        # then we match orders from the outside in, ensuring the fewest number of orders are amended and only
        # a new order is created in the inside. If we did it inside-out, all orders would be amended
        # down and a new order would be created at the outside.
        for i in reversed(range(1, settings.ORDER_PAIRS + 1)):
            if not self.long_position_limit_exceeded():
                buy_orders.append(self.prepare_order(-i))
            if not self.short_position_limit_exceeded():
                sell_orders.append(self.prepare_order(i))

        return self.converge_orders(buy_orders, sell_orders)

    def prepare_order(self, index):
        """Create an order object."""

        if settings.RANDOM_ORDER_SIZE is True:
            quantity = random.randint(settings.MIN_ORDER_SIZE, settings.MAX_ORDER_SIZE)
        else:
            quantity = settings.ORDER_START_SIZE + ((abs(index) - 1) * settings.ORDER_STEP_SIZE)

        price = self.get_price_offset(index)

        return {'price': price, 'orderQty': quantity, 'side': "Buy" if index < 0 else "Sell"}

    def verify_orders_and_leverage(self):

        position = self.exchange.get_position()
        qty = position['currentQty']
        if "leverage" in position:
            leverage = position['leverage']
        
            existing_orders = self.exchange.get_orders()

            buys_matched = 0
            sells_matched = 0

            if qty != 0:

                # Check all existing orders and match them up with what we want to place.
                # If there's an open one, we might be able to amend it to fit what we want.
                # for order in existing_orders:
                #     if order['side'] == 'Buy':
                #         buys_matched += 1
                #     if order['side'] == 'Sell':
                #         sells_matched += 1

                #margin_limit = position["markPrice"] - 50

                # if qty > 0: 
                #     total = position["markPrice"] - position["liquidationPrice"]
                #     if total < 300:
                #         leverage -= leverage * 0.3
                #         self.exchange.isolate_margin(self.exchange.symbol, leverage ,True)
                #         print("setting leverage: "+str(leverage))
                #     elif total > 300:
                #         if leverage > settings.LEVERAGE:
                #             leverage = settings.LEVERAGE
                #             self.exchange.isolate_margin(self.exchange.symbol, leverage ,True)
                #         elif leverage < settings.LEVERAGE:
                #             leverage += leverage * 0.01
                #             self.exchange.isolate_margin(self.exchange.symbol, leverage ,True)

                # if qty < 0:

                #     total = position["liquidationPrice"] - position["markPrice"]
                #     if total < 300:
                #         leverage -= leverage * 0.3
                #         self.exchange.isolate_margin(self.exchange.symbol, leverage ,True)
                #         print("setting leverage: "+str(leverage))
                #     elif total > 300:
                #         if leverage > settings.LEVERAGE:
                #             leverage = settings.LEVERAGE
                #             self.exchange.isolate_margin(self.exchange.symbol, leverage ,True)
                #         elif leverage < settings.LEVERAGE:
                #             leverage += leverage * 0.01
                #             self.exchange.isolate_margin(self.exchange.symbol, leverage ,True)

                print("leverage: "+str(leverage))

                # if qty > 0: 
                #     if position["markPrice"] <= position["liquidationPrice"] + 50 and position["markPrice"] < position["avgEntryPrice"]:
                #         leverage -= leverage * 0.3
                #         self.exchange.isolate_margin(self.exchange.symbol, leverage ,True)

                #     elif position["markPrice"] > position["liquidationPrice"] + 50 :
                #         leverage += leverage * 0.03
                #         self.exchange.isolate_margin(self.exchange.symbol, leverage ,True)

                # if qty < 0: 
                #     if position["markPrice"] >= position["liquidationPrice"] - 50 and position["markPrice"] > position["avgEntryPrice"]:
                #         leverage -= leverage * 0.3
                #         self.exchange.isolate_margin(self.exchange.symbol,leverage,True)

                #     elif position["markPrice"] < position["liquidationPrice"] - 50 :
                #         leverage += leverage * 0.01
                #         self.exchange.isolate_margin(self.exchange.symbol, leverage ,True)

                if leverage > self.leverage:
                    self.exchange.isolate_margin(self.exchange.symbol, self.leverage ,True)


    def converge_orders(self, buy_orders, sell_orders):
        """Converge the orders we currently have in the book with what we want to be in the book.
           This involves amending any open orders and creating new ones if any have filled completely.
           We start from the closest orders outward."""

        position = self.exchange.get_position()
        tickLog = self.exchange.get_instrument()['tickLog']
        to_amend = []
        to_create = []
        to_cancel = []
        buys_matched = 0
        sells_matched = 0
        existing_orders = self.exchange.get_orders()

        liqPrice = position['liquidationPrice'] if 'liquidationPrice' in position and position['liquidationPrice'] is not None else None
        currentQty = position['currentQty'] if position['currentQty'] != 0 else None

        # Check all existing orders and match them up with what we want to place.
        # If there's an open one, we might be able to amend it to fit what we want.
        for order in existing_orders:
            try:
                if order['side'] == 'Buy':
                    desired_order = buy_orders[buys_matched]
                    buys_matched += 1
                else:
                    desired_order = sell_orders[sells_matched]
                    sells_matched += 1

                # Found an existing order. Do we need to amend it?
                if desired_order['orderQty'] != order['leavesQty'] or (
                        # If price has changed, and the change is more than our RELIST_INTERVAL, amend.                        
                        desired_order['price'] != order['price'] and
                        abs((desired_order['price'] / order['price']) - 1) > settings.RELIST_INTERVAL):
                    to_amend.append({'orderID': order['orderID'], 'orderQty': order['cumQty'] + desired_order['orderQty'],
                                     'price': desired_order['price'], 'side': order['side']})
            except IndexError:
                # Will throw if there isn't a desired order to match. In that case, cancel it.
                to_cancel.append(order)

        while buys_matched < len(buy_orders):
            if position['currentQty'] != 0:
                if (position['currentQty'] < 0 or position['liquidationPrice'] < buy_orders[buys_matched]['price']):
                    to_create.append(buy_orders[buys_matched])
            elif position['currentQty'] == 0:
                to_create.append(buy_orders[buys_matched])
            buys_matched += 1

        while sells_matched < len(sell_orders):
            if position['currentQty'] != 0:
                if position['currentQty'] > 0 or position['liquidationPrice'] > sell_orders[sells_matched]['price']:
                    to_create.append(sell_orders[sells_matched])
            elif position['currentQty'] == 0:
                to_create.append(sell_orders[sells_matched])
            sells_matched += 1

        if len(to_amend) > 0:
            for amended_order in reversed(to_amend):
                reference_order = [o for o in existing_orders if o['orderID'] == amended_order['orderID']][0]
                logger.info("Amending %4s: %d @ %.*f to %d @ %.*f (%+.*f)" % (
                    amended_order['side'],
                    reference_order['leavesQty'], tickLog, reference_order['price'],
                    (amended_order['orderQty'] - reference_order['cumQty']), tickLog, amended_order['price'],
                    tickLog, (amended_order['price'] - reference_order['price'])
                ))
            # This can fail if an order has closed in the time we were processing.
            # The API will send us `invalid ordStatus`, which means that the order's status (Filled/Canceled)
            # made it not amendable.
            # If that happens, we need to catch it and re-tick.
            try:
                self.exchange.amend_orders(to_amend,liqPrice, currentQty)
            except requests.exceptions.HTTPError as e:
                errorObj = e.response.json()
                if errorObj['error']['message'] == 'Invalid ordStatus':
                    logger.warn("Amending failed. Waiting for order data to converge and retrying.")
                    sleep(0.5)
                    return self.place_orders()
                else:
                    logger.error("Unknown error on amend: %s. Exiting" % errorObj)
                    # sys.exit(1)

        if len(to_create) > 0:
            logger.info("Creating %d orders:" % (len(to_create)))
            for order in reversed(to_create):
                logger.info("%4s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))            
            
            self.exchange.isolate_margin(self.exchange.symbol, settings.LEVERAGE ,True)
            self.exchange.create_orders(to_create, liqPrice, currentQty)

        # Could happen if we exceed a delta limit
        if len(to_cancel) > 0:
            logger.info("Canceling %d orders:" % (len(to_cancel)))
            for order in reversed(to_cancel):
                logger.info("%4s %d @ %.*f" % (order['side'], order['leavesQty'], tickLog, order['price']))
            self.exchange.cancel_orders(to_cancel)

    ###
    # Position Limits
    ###

    def short_position_limit_exceeded(self):
        """Returns True if the short position limit is exceeded"""
        if not settings.CHECK_POSITION_LIMITS:
            return False
        position = self.exchange.get_delta()
        return position <= settings.MIN_POSITION

    def long_position_limit_exceeded(self):
        """Returns True if the long position limit is exceeded"""
        if not settings.CHECK_POSITION_LIMITS:
            return False
        position = self.exchange.get_delta()
        return position >= settings.MAX_POSITION

    ###
    # Sanity
    ##

    def sanity_check(self):
        """Perform checks before placing orders."""

        # Check if OB is empty - if so, can't quote.
        self.exchange.check_if_orderbook_empty()

        # Ensure market is still open.
        self.exchange.check_market_open()

        # Get ticker, which sets price offsets and prints some debugging info.
        ticker = self.get_ticker()

        # Sanity check:        
        if self.get_price_offset(-1) >= ticker["sell"] or self.get_price_offset(1) <= ticker["buy"]:
            logger.error("Buy: %s, Sell: %s" % (self.start_position_buy, self.start_position_sell))
            logger.error("First buy position: %s\nBitMEX Best Ask: %s\nFirst sell position: %s\nBitMEX Best Bid: %s" %
                         (self.get_price_offset(-1), ticker["sell"], self.get_price_offset(1), ticker["buy"]))
            logger.error("Sanity check failed, exchange data is inconsistent")
            self.exit()
      

        # Messaging if the position limits are reached
        if self.long_position_limit_exceeded():
            logger.info("Long delta limit exceeded")
            logger.info("Current Position: %.f, Maximum Position: %.f" %
                        (self.exchange.get_delta(), settings.MAX_POSITION))

        if self.short_position_limit_exceeded():
            logger.info("Short delta limit exceeded")
            logger.info("Current Position: %.f, Minimum Position: %.f" %
                        (self.exchange.get_delta(), settings.MIN_POSITION))

    ###
    # Running
    ###

    def check_file_change(self):
        """Restart if any files we're watching have changed."""
        for f, mtime in watched_files_mtimes:
            if getmtime(f) > mtime:
                self.restart()

    def check_connection(self):
        """Ensure the WS connections are still open."""
        return self.exchange.is_open()

    def exit(self):
        logger.info("Shutting down. All open orders will be cancelled.")
        try:
            self.exchange.cancel_all_orders()
            self.exchange.bitmex.exit()
        except errors.AuthenticationError as e:
            logger.info("Was not authenticated; could not cancel orders.")
        except Exception as e:
            logger.info("Unable to cancel orders: %s" % e)

        sys.exit()

    def run_loop(self):
        while True:
            sys.stdout.write("-----\n")
            sys.stdout.flush()

            self.check_file_change()
            sleep(settings.LOOP_INTERVAL)

            # This will restart on very short downtime, but if it's longer,
            # the MM will crash entirely as it is unable to connect to the WS on boot.
            if not self.check_connection():
                logger.error("Realtime data connection unexpectedly closed, restarting.")
                self.restart()

            self.sanity_check()  # Ensures health of mm - several cut-out points here
            self.print_status()  # Print skew, delta, etc            
            self.place_orders()  # Creates desired orders and converges to existing orders         
            #self.initialize_position() #Initialize a position   
            self.verify_leverage() #Set the correct leverage value avoiding Bitmex auto set on order execution and liquidations
            self.verify_orders_and_leverage() #Verify number of order of the same side and adjust leverage to avoid liquidations
            self.verify_profit() # Realize if are profitble

    def restart(self):
        logger.info("Restarting the market maker...")
        os.execv(sys.executable, [sys.executable] + sys.argv)

#
# Helpers
#

def set_long():
    global long_enable
    global short_enable

    """ exchange = ExchangeInterface(settings.DRY_RUN)

    position = exchange.get_position()

    roe = position['unrealisedRoePcnt']
    pnl_percent = position['unrealisedPnlPcnt']
    pnl = position['unrealisedGrossPnl']
    qty = position['currentQty']

    if short_enable == True and qty != 0:
        logger.info("Aproximated realized (Market Price) PNL: %.*f" % (3, float(pnl))) 
        exchange.close_position(float(qty) * -1)
        logger.info("ROE realized: %.*f" % (3, float(roe))) """

    long_enable = True
    short_enable = False

def set_short():
    global long_enable
    global short_enable

    """ exchange = ExchangeInterface(settings.DRY_RUN)

    position = exchange.get_position()
    
    roe = position['unrealisedRoePcnt']
    pnl_percent = position['unrealisedPnlPcnt']
    pnl = position['unrealisedGrossPnl']
    qty = position['currentQty']

    if long_enable == True and qty != 0:
        logger.info("Aproximated realized (Market Price) PNL: %.*f" % (3, float(pnl))) 
        exchange.close_position(float(qty) * -1)
        logger.info("ROE realized: %.*f" % (3, float(roe))) """

    short_enable = True
    long_enable = False


def XBt_to_XBT(XBt):
    return float(XBt) / constants.XBt_TO_XBT


def cost(instrument, quantity, price):
    mult = instrument["multiplier"]
    P = mult * price if mult >= 0 else mult / price
    return abs(quantity * P)


def margin(instrument, quantity, price):
    return cost(instrument, quantity, price) * instrument["initMargin"]


def run():
    logger.info('BitMEX Market Maker Version: %s\n' % constants.VERSION)

    om = OrderManager()
    # Try/except just keeps ctrl-c from printing an ugly stacktrace
    try:
        om.run_loop()
    except (KeyboardInterrupt, SystemExit):
        sys.exit()

