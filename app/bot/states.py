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
