from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.banking.models import Institution
from apps.providers.prices.registry import get as get_price_provider
from apps.providers.registry import get as get_provider

from .models import Holding, InvestmentAccount, PortfolioSnapshot


@dataclass
class InvestmentSyncResult:
    accounts_created: int
    accounts_updated: int
    holdings_created: int
    holdings_updated: int
    holdings_manual_basis_preserved: int


def sync_simplefin_investments(institution: Institution) -> InvestmentSyncResult:
    provider = get_provider(institution.provider)

    ac_created = ac_updated = 0
    h_created = h_updated = h_manual_preserved = 0

    with transaction.atomic():
        for payload in provider.fetch_investment_accounts(institution.access_url):
            acc, created = InvestmentAccount.objects.update_or_create(
                institution=institution,
                external_id=payload.external_id,
                defaults={
                    "user": institution.user,
                    "source": "simplefin",
                    "broker": payload.broker,
                    "name": payload.name,
                    "currency": payload.currency,
                    "last_synced_at": timezone.now(),
                },
            )
            if created:
                ac_created += 1
            else:
                ac_updated += 1

            for hd in payload.holdings:
                existing = Holding.objects.filter(
                    investment_account=acc, external_id=hd.external_id,
                ).first()

                preserve_manual_basis = bool(existing and existing.cost_basis_source == "manual")
                cost_basis = existing.cost_basis if preserve_manual_basis else hd.cost_basis
                cost_basis_source = "manual" if preserve_manual_basis else ("auto" if hd.cost_basis is not None else "auto")

                _, h_was_created = Holding.objects.update_or_create(
                    investment_account=acc,
                    external_id=hd.external_id,
                    defaults={
                        "symbol": hd.symbol,
                        "description": hd.description,
                        "shares": hd.shares,
                        "current_price": hd.current_price,
                        "market_value": hd.market_value,
                        "cost_basis": cost_basis,
                        "cost_basis_source": cost_basis_source,
                        "last_priced_at": timezone.now(),
                    },
                )
                if h_was_created:
                    h_created += 1
                else:
                    h_updated += 1
                if preserve_manual_basis:
                    h_manual_preserved += 1

            _snapshot_total(acc)

    return InvestmentSyncResult(
        accounts_created=ac_created,
        accounts_updated=ac_updated,
        holdings_created=h_created,
        holdings_updated=h_updated,
        holdings_manual_basis_preserved=h_manual_preserved,
    )


def create_manual_account(*, user, broker: str, name: str, notes: str = "") -> InvestmentAccount:
    return InvestmentAccount.objects.create(
        user=user,
        source="manual",
        broker=broker,
        name=name,
        notes=notes,
    )


def upsert_manual_holding(
    *,
    investment_account: InvestmentAccount,
    symbol: str,
    shares: Decimal,
    cost_basis: Decimal | None,
) -> Holding:
    assert investment_account.source == "manual", "upsert_manual_holding only valid for manual accounts"
    symbol = symbol.strip().upper()
    holding, _ = Holding.objects.update_or_create(
        investment_account=investment_account,
        external_id="",
        symbol=symbol,
        defaults={
            "shares": shares,
            "cost_basis": cost_basis,
            "cost_basis_source": "manual" if cost_basis is not None else "auto",
        },
    )
    holding.recompute_market_value()
    holding.save(update_fields=["market_value"])
    return holding


def update_cost_basis(*, holding: Holding, cost_basis: Decimal | None) -> Holding:
    holding.cost_basis = cost_basis
    holding.cost_basis_source = "manual" if cost_basis is not None else "auto"
    holding.save(update_fields=["cost_basis", "cost_basis_source"])
    return holding


def refresh_manual_prices(*, user) -> int:
    """Fetch live prices (default: Stooq) for every manual holding symbol this user owns.
    Returns the number of holdings whose price was updated.
    """
    manual_holdings = list(
        Holding.objects
        .filter(investment_account__user=user, investment_account__source="manual")
        .select_related("investment_account")
    )
    symbols = sorted({h.symbol for h in manual_holdings if h.symbol})
    if not symbols:
        return 0

    quotes = {q.symbol: q for q in get_price_provider().fetch_quotes(symbols)}
    now = timezone.now()
    updated = 0
    account_ids = {h.investment_account_id for h in manual_holdings}

    with transaction.atomic():
        for h in manual_holdings:
            quote = quotes.get(h.symbol)
            if quote is None:
                continue
            h.current_price = quote.price
            h.last_priced_at = now
            h.recompute_market_value()
            h.save(update_fields=["current_price", "market_value", "last_priced_at"])
            updated += 1

        # Snapshot every manual account on each run — even those with no price
        # update — using the holdings' last-known prices. Gating snapshots on a
        # successful fetch let one provider outage permanently flatline the chart.
        for acc_id in account_ids:
            acc = InvestmentAccount.objects.get(pk=acc_id)
            _snapshot_total(acc)

    return updated


def _snapshot_total(account: InvestmentAccount) -> None:
    total = sum(
        (h.market_value for h in account.holdings.all()),
        start=Decimal("0"),
    ) + account.cash_balance
    PortfolioSnapshot.objects.update_or_create(
        investment_account=account,
        date=date.today(),
        defaults={"total_value": total},
    )
