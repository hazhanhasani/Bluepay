from aiogram.fsm.state import State, StatesGroup


class AddCardState(StatesGroup):
    bank = State()
    card_number = State()
    holder = State()
    source_id = State()


class ManualInvoiceState(StatesGroup):
    amount = State()
    description = State()
    fee_mode = State()
    card = State()


class AdminWalletAdjustState(StatesGroup):
    amount = State()


class AdminFeeState(StatesGroup):
    amount = State()


class AdminSmsApproveState(StatesGroup):
    invoice_token = State()
