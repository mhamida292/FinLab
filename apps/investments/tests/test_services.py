from datetime import date, datetime, timezone as tz
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from apps.banking.models import Institution
from apps.investments.models import Holding, InvestmentAccount, PortfolioSnapshot
from apps.investments.services import (
    create_manual_account, refresh_manual_prices, sync_simplefin_investments,
    update_cost_basis, upsert_manual_holding,
)
from apps.providers import registry as provider_registry
from apps.providers.base import HoldingData, InvestmentAccountSyncPayload
from apps.providers.prices import registry as price_registry
from apps.providers.prices.base import PriceQuote

User = get_user_model()


class _FakeSimpleFIN:
    name = "simplefin"

    def __init__(self, payloads):
        self._payloads = payloads

    def exchange_setup_token(self, setup_token):
        return "https://FAKE/simplefin"

    def fetch_accounts_with_transactions(self, access_url):
        return iter(())

    def fetch_investment_accounts(self, access_url):
        yield from self._payloads


class _FakePriceProvider:
    name = "yahoo_chart"
    quotes_by_symbol: dict[str, Decimal] = {}

    def fetch_quotes(self, symbols):
        now = datetime.now(tz=tz.utc)
        return [
            PriceQuote(symbol=s.upper(), price=self.quotes_by_symbol[s.upper()], at=now)
            for s in symbols
            if s.upper() in self.quotes_by_symbol
        ]


@pytest.fixture
def fake_simplefin_single_holding():
    payloads = [
        InvestmentAccountSyncPayload(
            external_id="INV-1", name="Roth IRA", broker="Robinhood", currency="USD",
            holdings=(
                HoldingData(
                    external_id="H-1", symbol="AAPL", description="Apple",
                    shares=Decimal("10"), current_price=Decimal("180"),
                    market_value=Decimal("1800"), cost_basis=Decimal("1500"),
                ),
            ),
        ),
    ]
    original = provider_registry._REGISTRY.copy()
    provider_registry._REGISTRY["simplefin"] = lambda: _FakeSimpleFIN(payloads)
    yield
    provider_registry._REGISTRY.clear()
    provider_registry._REGISTRY.update(original)


@pytest.fixture
def fake_yahoo():
    original = price_registry._REGISTRY.copy()
    _FakePriceProvider.quotes_by_symbol = {}
    price_registry._REGISTRY["yahoo_chart"] = _FakePriceProvider
    yield _FakePriceProvider
    price_registry._REGISTRY.clear()
    price_registry._REGISTRY.update(original)


@pytest.mark.django_db
def test_sync_simplefin_investments_creates_account_and_holdings(fake_simplefin_single_holding):
    user = User.objects.create_user(username="alice", password="correct-horse-battery-staple")
    inst = Institution.objects.create(user=user, name="Brokerage", access_url="https://FAKE")

    result = sync_simplefin_investments(inst)

    assert result.accounts_created == 1
    assert result.holdings_created == 1
    assert InvestmentAccount.objects.filter(institution=inst).count() == 1
    inv = InvestmentAccount.objects.get(institution=inst)
    assert inv.user == user
    assert inv.source == "simplefin"

    h = Holding.objects.get(investment_account=inv)
    assert h.symbol == "AAPL"
    assert h.cost_basis == Decimal("1500")
    assert h.cost_basis_source == "auto"
    snap = PortfolioSnapshot.objects.get(investment_account=inv, date=date.today())
    assert snap.total_value == Decimal("1800")


@pytest.mark.django_db
def test_sync_preserves_manual_cost_basis(fake_simplefin_single_holding):
    user = User.objects.create_user(username="alice", password="correct-horse-battery-staple")
    inst = Institution.objects.create(user=user, name="B", access_url="https://FAKE")

    sync_simplefin_investments(inst)
    h = Holding.objects.get()
    update_cost_basis(holding=h, cost_basis=Decimal("2000"))
    assert h.cost_basis_source == "manual"

    result = sync_simplefin_investments(inst)

    h.refresh_from_db()
    assert h.cost_basis == Decimal("2000"), "Manual basis must survive sync"
    assert h.cost_basis_source == "manual"
    assert result.holdings_manual_basis_preserved == 1


@pytest.mark.django_db
def test_create_manual_account_and_holding():
    user = User.objects.create_user(username="alice", password="correct-horse-battery-staple")
    acc = create_manual_account(user=user, broker="Fidelity", name="401k")
    assert acc.source == "manual"

    h = upsert_manual_holding(
        investment_account=acc, symbol="vti",
        shares=Decimal("40"), cost_basis=Decimal("8000"),
    )
    assert h.symbol == "VTI"
    assert h.cost_basis_source == "manual"
    assert h.market_value == Decimal("0.00")


@pytest.mark.django_db
def test_upsert_manual_holding_updates_existing_symbol():
    user = User.objects.create_user(username="alice", password="correct-horse-battery-staple")
    acc = create_manual_account(user=user, broker="Fidelity", name="401k")
    upsert_manual_holding(investment_account=acc, symbol="VTI", shares=Decimal("40"), cost_basis=Decimal("8000"))
    upsert_manual_holding(investment_account=acc, symbol="VTI", shares=Decimal("50"), cost_basis=Decimal("9500"))

    assert Holding.objects.filter(investment_account=acc, symbol="VTI").count() == 1
    h = Holding.objects.get(investment_account=acc, symbol="VTI")
    assert h.shares == Decimal("50")
    assert h.cost_basis == Decimal("9500")


@pytest.mark.django_db
def test_refresh_manual_prices_updates_only_manual_holdings(fake_yahoo):
    user = User.objects.create_user(username="alice", password="correct-horse-battery-staple")
    acc = create_manual_account(user=user, broker="Fidelity", name="401k")
    upsert_manual_holding(investment_account=acc, symbol="VTI", shares=Decimal("40"), cost_basis=None)

    fake_yahoo.quotes_by_symbol = {"VTI": Decimal("250.00")}
    updated = refresh_manual_prices(user=user)

    assert updated == 1
    h = Holding.objects.get(symbol="VTI")
    assert h.current_price == Decimal("250.0000")
    assert h.market_value == Decimal("10000.00")
    snap = PortfolioSnapshot.objects.get(investment_account=acc, date=date.today())
    assert snap.total_value == Decimal("10000.00")


@pytest.mark.django_db
def test_refresh_manual_prices_snapshots_even_when_no_quotes(fake_yahoo):
    """A price-provider outage must not stall the portfolio history.

    When the provider returns zero quotes, we still write a snapshot for each
    manual account using its holdings' last-known stored prices — otherwise a
    single bad fetch permanently flatlines the chart (the Stooq-retirement bug).
    """
    user = User.objects.create_user(username="alice", password="correct-horse-battery-staple")
    acc = create_manual_account(user=user, broker="Fidelity", name="401k")
    h = upsert_manual_holding(investment_account=acc, symbol="VTI", shares=Decimal("40"), cost_basis=None)
    # Simulate a prior successful refresh having stored a price.
    h.current_price = Decimal("250.00")
    h.recompute_market_value()
    h.save(update_fields=["current_price", "market_value"])

    # Provider returns nothing this run (outage / retired endpoint).
    fake_yahoo.quotes_by_symbol = {}
    updated = refresh_manual_prices(user=user)

    assert updated == 0
    snap = PortfolioSnapshot.objects.get(investment_account=acc, date=date.today())
    assert snap.total_value == Decimal("10000.00")
