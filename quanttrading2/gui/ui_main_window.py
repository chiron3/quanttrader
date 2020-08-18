#!/usr/bin/env python
# -*- coding: utf-8 -*-
# http://stackoverflow.com/questions/9957195/updating-gui-elements-in-multithreaded-pyqt
import sys
import os
import webbrowser
import psutil
from queue import Queue, Empty
from copy import copy
import logging
from PyQt5 import QtCore, QtWidgets, QtGui
from datetime import datetime

from ..brokerage.ib_brokerage import InteractiveBrokers
from ..event.event import EventType
from ..order.order_flag import OrderFlag
from ..data.data_board import DataBoard
from ..order.order_manager import OrderManager
from ..strategy.strategy_manager import StrategyManager
from ..position.position_manager import PositionManager
from ..risk.risk_manager import PassThroughRiskManager
from ..account.account_manager import AccountManager
from ..event.live_event_engine import LiveEventEngine
from ..order.order_event import OrderEvent
from ..order.order_type import OrderType
from .ui_order_window import OrderWindow
from .ui_fill_window import FillWindow
from .ui_position_window import PositionWindow
from .ui_account_window import AccountWindow
from .ui_strategy_window import StrategyWindow
from .ui_log_window import LogWindow
from .ui_trade_menu import TradeMenu

_logger = logging.getLogger(__name__)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, config, strat_dict):
        super(MainWindow, self).__init__()

        ## member variables
        self._current_time = None
        self._config = config
        self.multiplier_dict = dict()
        self.central_widget = None
        self.message_window = None
        self.order_window = None
        self.fill_window = None
        self.position_window = None
        self.account_window = None
        self.strategy_window = None

        self._msg_events_engine = LiveEventEngine()  # msg engine
        self._tick_events_engine = LiveEventEngine()  # tick data engine
        self._broker = InteractiveBrokers(self._msg_events_engine, self._tick_events_engine, self._config['account'])
        self._position_manager = PositionManager()      # global position manager
        self._position_manager.set_multiplier(self.multiplier_dict)
        self._order_manager = OrderManager()          # global order manager
        self._data_board = DataBoard()
        self.risk_manager = PassThroughRiskManager()
        self.account_manager = AccountManager(self._config['account'])

        self._strategy_manager = StrategyManager(self._config, self._broker, self._order_manager, self._position_manager, self.risk_manager, self._data_board, self.multiplier_dict)
        self._load_strategy(strat_dict)

        self.widgets = dict()
        self._schedule_timer = QtCore.QTimer()                  # task scheduler; TODO produce result_packet

        # set up gui windows
        self.setGeometry(50, 50, 600, 400)
        self.setWindowTitle('QuantTrading')
        self.setWindowIcon(QtGui.QIcon("gui/image/logo.ico"))
        self.init_menu()
        self.init_status_bar()
        self.init_central_area()

        ## wire up event handlers
        self._tick_events_engine.register_handler(EventType.TICK, self._tick_event_handler)
        self._msg_events_engine.register_handler(EventType.ORDER, self._order_status_event_handler)
        self._msg_events_engine.register_handler(EventType.FILL, self._fill_event_handler)
        self._msg_events_engine.register_handler(EventType.POSITION, self._position_event_handler)
        self._msg_events_engine.register_handler(EventType.ACCOUNT, self.account_window.account_signal.emit)
        self._msg_events_engine.register_handler(EventType.CONTRACT, self._contract_event_handler)
        self._msg_events_engine.register_handler(EventType.HISTORICAL, self._historical_event_handler)
        self._msg_events_engine.register_handler(EventType.LOG, self.log_window.msg_signal.emit)

        ## start
        self._msg_events_engine.start()
        self._tick_events_engine.start()

        self.connect_to_broker()

    def _load_strategy(self, strat_dict):
        i = 1   # 0 is mannual discretionary trade, or not found
        # similar to backtest; strategy sets capital, params, and symbols
        for k, v in strat_dict.items():
            v.id = i
            v.name = k
            v.active = self._config['strategy'][v.name]['active']
            v.set_capital(self._config['strategy'][v.name]['capital'])  # float
            v.set_params(self._config['strategy'][v.name]['params'])  # dict
            v.set_symbols(self._config['strategy'][v.name]['symbols'])  # list
            i += 1
        self._strategy_manager.load_strategy(strat_dict)

    #################################################################################################
    # -------------------------------- Event Handler   --------------------------------------------#
    #################################################################################################
    def connect_to_broker(self):
        self._broker.connect(self._config['host'], self._config['port'], self._config['client_id'])

    def disconnect_from_broker(self):
        self._broker.disconnect()

    def open_trade_widget(self):
        widget = self.widgets.get('trade_menu', None)
        if not widget:
            widget = TradeMenu(self._broker, self._msg_events_engine, self._strategy_manager._multiplier_dict)
            self.widgets['trade_menu'] = widget
        widget.show()

    def update_status_bar(self, message):
        self.statusBar().showMessage(message)
        self.strategy_window.update_pnl()        # pnl update

    def start_strategy(self):
        try:
            sid_txt = self.strategy_window.item(self.strategy_window.currentRow(), 0).text()
            _logger.info(f'control: start_strategy {sid_txt}')
            self.strategy_window.update_status(self.strategy_window.currentRow(), True)
        except:
            _logger.error(f'control: start_strategy error, no row selected')

    def stop_strategy(self):
        try:
            sid_txt = self.strategy_window.item(self.strategy_window.currentRow(), 0).text()
            _logger.info(f'control: stop_strategy {sid_txt}')
            self.strategy_window.update_status(self.strategy_window.currentRow(), False)
        except:
            _logger.error(f'control: stop_strategy error, no row selected')

    def liquidate_strategy(self):
        try:
            sid_txt = self.strategy_window.item(self.strategy_window.currentRow(), 0).text()
            _logger.info(f'control: liquidate_strategy {sid_txt}')
            sid = int(self.strategy_window.item(self.strategy_window.currentRow(), 0).text())
            self._strategy_manager.flat_strategy(sid)
        except:
            _logger.error(f'control: liquidate_strategy error, no row selected')

    def start_all_strategy(self):
        _logger.info(f'control: start all strategy')
        self._strategy_manager.start_all()
        for i in range(self.strategy_window.rowCount()):
            self.strategy_window.setItem(i, 7, QtWidgets.QTableWidgetItem('active'))

    def stop_all_strategy(self):
        _logger.info(f'control: stop all strategy')
        self._strategy_manager.stop_all()
        for i in range(self.strategy_window.rowCount()):
            self.strategy_window.setItem(i, 7, QtWidgets.QTableWidgetItem('inactive'))

    def liquidate_all_strategy(self):
        _logger.info(f'control: liquidate all strategy')
        self._strategy_manager.flat_all()

    def closeEvent(self, a0: QtGui.QCloseEvent):
        _logger.info('closing main window')
        self.disconnect_from_broker()
        self._msg_events_engine.stop()
        self._tick_events_engine.stop()

    def _tick_event_handler(self, tick_event):
        self._current_time = tick_event.timestamp

        self._strategy_manager.on_tick(tick_event)  # feed strategies
        self._position_manager.mark_to_market(tick_event.timestamp, tick_event.full_symbol, tick_event.price, self._data_board)
        self._data_board.on_tick(tick_event)       # update databoard

    def _order_status_event_handler(self, order_event):  # including cancel
        # self._order_manager.on_order_status(order_event)     # this moves to order_window to tell it to update
        self.order_window.order_status_signal.emit(copy(order_event))    # NEED TO MAKE A COPY
        self._strategy_manager.on_order_status(order_event)
        self.strategy_window.update_order(order_event)

    def _fill_event_handler(self, fill_event):
        self._position_manager.on_fill(fill_event)   # update portfolio manager for pnl
        self._order_manager.on_fill(fill_event)  # update order manager with fill
        self._strategy_manager.on_fill(fill_event)  # feed fill to strategy
        self.fill_window.fill_signal.emit(fill_event)     # display
        self.order_window.update_order_status(fill_event.order_id)        # let order_window listen to fill as well
        self.strategy_window.update_fill(fill_event)

    def _position_event_handler(self, position_event):
        self._position_manager.on_position(position_event)       # position received
        self.position_window.position_signal.emit(position_event)     # display

    def _account_event_handler(self, account_event):
        pass

    def _contract_event_handler(self, contract_event):
        self._position_manager.on_contract(contract_event)

    def _historical_event_handler(self, historical_event):
        pass

    #################################################################################################
    # ------------------------------ Event Handler Ends --------------------------------------------#
    #################################################################################################

    #################################################################################################
    # -------------------------------- User Interface  --------------------------------------------#
    #################################################################################################
    def init_menu(self):
        menubar = self.menuBar()

        sysMenu = menubar.addMenu('Menu')
        sys_tradeAction = QtWidgets.QAction('Trade', self)
        sys_tradeAction.setStatusTip('Manual Trade')
        sys_tradeAction.triggered.connect(self.open_trade_widget)
        sysMenu.addAction(sys_tradeAction)

        sysMenu.addSeparator()

        # sys|exit
        sys_exitAction = QtWidgets.QAction('Exit', self)
        sys_exitAction.setShortcut('Ctrl+Q')
        sys_exitAction.setStatusTip('Exit_App')
        sys_exitAction.triggered.connect(self.close)
        sysMenu.addAction(sys_exitAction)

    def init_status_bar(self):
        self.statusthread = StatusThread()
        self.statusthread.status_update.connect(self.update_status_bar)
        self.statusthread.start()

    def init_central_area(self):
        self.central_widget = QtWidgets.QWidget()

        hbox = QtWidgets.QHBoxLayout()

        # -------------------------------- top ------------------------------------------#
        top = QtWidgets.QFrame()
        top.setFrameShape(QtWidgets.QFrame.StyledPanel)
        control_layout = QtWidgets.QHBoxLayout()
        self.btn_strat_start = QtWidgets.QPushButton('Start_Strat')
        self.btn_strat_start.clicked.connect(self.start_strategy)
        self.btn_strat_stop = QtWidgets.QPushButton('Stop_Strat')
        self.btn_strat_stop.clicked.connect(self.stop_strategy)
        self.btn_strat_liquidate = QtWidgets.QPushButton('Liquidate_Strat')
        self.btn_strat_liquidate.clicked.connect(self.liquidate_strategy)
        self.btn_all_start = QtWidgets.QPushButton('Start_All')
        self.btn_all_start.clicked.connect(self.start_all_strategy)
        self.btn_all_stop = QtWidgets.QPushButton('Stop_All')
        self.btn_all_stop.clicked.connect(self.stop_all_strategy)
        self.btn_all_liquidate = QtWidgets.QPushButton('Liquidate_All')
        self.btn_all_liquidate.clicked.connect(self.liquidate_all_strategy)
        control_layout.addWidget(self.btn_strat_start)
        control_layout.addWidget(self.btn_strat_stop)
        control_layout.addWidget(self.btn_strat_liquidate)
        control_layout.addWidget(self.btn_all_start)
        control_layout.addWidget(self.btn_all_stop)
        control_layout.addWidget(self.btn_all_liquidate)

        top.setLayout(control_layout)
        # -------------------------------- Bottom ------------------------------------------#
        bottom = QtWidgets.QTabWidget()
        tab1 = QtWidgets.QWidget()
        tab2 = QtWidgets.QWidget()
        tab3 = QtWidgets.QWidget()
        tab4 = QtWidgets.QWidget()
        tab5 = QtWidgets.QWidget()
        tab6 = QtWidgets.QWidget()
        bottom.addTab(tab1, 'Strategy')
        bottom.addTab(tab2, 'Order')
        bottom.addTab(tab3, 'Fill')
        bottom.addTab(tab4, 'Position')
        bottom.addTab(tab5, 'Account')
        bottom.addTab(tab6, 'Log')

        self.strategy_window = StrategyWindow(self._strategy_manager)
        tab1_layout = QtWidgets.QVBoxLayout()
        tab1_layout.addWidget(self.strategy_window)
        tab1.setLayout(tab1_layout)

        self.order_window = OrderWindow(self._order_manager, self._broker)       # cancel_order outgoing nessage
        tab2_layout = QtWidgets.QVBoxLayout()
        tab2_layout.addWidget(self.order_window)
        tab2.setLayout(tab2_layout)

        self.fill_window =FillWindow()
        tab3_layout = QtWidgets.QVBoxLayout()
        tab3_layout.addWidget(self.fill_window)
        tab3.setLayout(tab3_layout)

        self.position_window = PositionWindow()
        tab4_layout = QtWidgets.QVBoxLayout()
        tab4_layout.addWidget(self.position_window)
        tab4.setLayout(tab4_layout)

        self.account_window = AccountWindow(self.account_manager)
        tab5_layout = QtWidgets.QVBoxLayout()
        tab5_layout.addWidget(self.account_window)
        tab5.setLayout(tab5_layout)

        self.log_window = LogWindow()
        tab6_layout = QtWidgets.QVBoxLayout()
        tab6_layout.addWidget(self.log_window)
        tab6.setLayout(tab6_layout)

        # --------------------------------------------------------------------------------------#
        splitter1 = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter1.addWidget(top)
        splitter1.addWidget(bottom)
        # splitter1.setSizes([10, 100])

        hbox.addWidget(splitter1)
        self.central_widget.setLayout(hbox)
        self.setCentralWidget(self.central_widget)

    #################################################################################################
    # ------------------------------ User Interface End --------------------------------------------#
    #################################################################################################

class StatusThread(QtCore.QThread):
    status_update = QtCore.pyqtSignal(str)

    def __init__(self):
        QtCore.QThread.__init__(self)

    def run(self):
        while True:
            cpuPercent = psutil.cpu_percent()
            memoryPercent = psutil.virtual_memory().percent
            self.status_update.emit('CPU Usage: ' + str(cpuPercent) + '% Memory Usage: ' + str(memoryPercent) + '%')
            self.sleep(2)