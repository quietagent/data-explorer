"""Active lines and their plans. A test fixture: the Scripts tab reads it as text and never runs it."""


def active_lines(cursor):
    cursor.execute(
        "SELECT s.msisdn, p.plan_name, p.monthly_fee "
        "FROM subscriptions s JOIN plans p ON p.plan_id = s.plan_id "
        "WHERE s.status = 'active'")
    return cursor.fetchall()
