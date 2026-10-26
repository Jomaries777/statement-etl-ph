"""Read Philippine bank and e-wallet statements into plain transactions.

    from statement_etl_ph.banks import get_bank

    statement = get_bank("gcash").parser("statement.pdf", "password")
    assert statement.reconcile().ok
    for txn in statement.transactions:
        print(txn.value_date, txn.signed_amount, txn.merchant)
"""

__version__ = "0.2.0"
