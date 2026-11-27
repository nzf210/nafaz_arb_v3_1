"""Telegram HTML message formatters and inline keyboard layouts for nafaz_arb v3.1."""

import html
import time
from typing import Any, Dict, List, Optional

MENU_UTAMA_TEXT = "📱 Menu Utama"
TEXT_SIMULASI = "SIMULASI"
TEXT_LIVE = "LIVE"
TEXT_USDT = "USDT"
SEPARATOR = "----------------------------------------\n"


def default_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "⚡ Status", "callback_data": "status"},
                {"text": "💼 Posisi Aktif", "callback_data": "positions"},
            ],
            [
                {"text": "💰 Cek Balance", "callback_data": "balance"},
                {"text": "📜 Riwayat", "callback_data": "history"},
            ],
            [
                {"text": "📋 Peluang Terbaru", "callback_data": "latest"},
                {"text": "📊 Statistik Metrik", "callback_data": "metrics"},
            ],
            [
                {"text": "⚙️ Pengaturan", "callback_data": "settings"},
                {"text": "🔔 Notifikasi", "callback_data": "notif"},
                {"text": "🛡️ Risk Status", "callback_data": "risk"},
            ],
            [
                {"text": "🔄 Refresh Menu", "callback_data": "menu"},
                {"text": "ℹ️ Bantuan", "callback_data": "help"},
            ],
        ]
    }


def settings_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🔔 Filter Notifikasi", "callback_data": "notif"},
                {"text": "🛑 Toggle Kill-Switch", "callback_data": "toggle_killswitch"},
            ],
            [
                {"text": "⚡ Status", "callback_data": "status"},
                {"text": MENU_UTAMA_TEXT, "callback_data": "menu"},
            ],
        ]
    }


def notif_keyboard(filters: Dict[str, bool]) -> Dict[str, Any]:
    def _lbl(name: str, key: str) -> str:
        return f"{name}: {'✅ ON' if filters.get(key, True) else '❌ OFF'}"

    return {
        "inline_keyboard": [
            [
                {"text": "🎯 Hanya Trade", "callback_data": "notif:trades_only"},
                {"text": "🔔 Semua ON", "callback_data": "notif:all_on"},
            ],
            [
                {"text": _lbl("Peluang", "opps"), "callback_data": "notif:toggle:opps"},
                {"text": _lbl("Scan Reject", "rejections"), "callback_data": "notif:toggle:rejections"},
            ],
            [
                {"text": _lbl("Order Fill", "fills"), "callback_data": "notif:toggle:fills"},
                {"text": _lbl("Posisi Close", "closes"), "callback_data": "notif:toggle:closes"},
            ],
            [
                {"text": _lbl("Error Alert", "errors"), "callback_data": "notif:toggle:errors"},
                {"text": "🔕 Mute Semua", "callback_data": "notif:all_off"},
            ],
            [
                {"text": "⚙️ Pengaturan", "callback_data": "settings"},
                {"text": MENU_UTAMA_TEXT, "callback_data": "menu"},
            ],
        ]
    }


def positions_keyboard(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows: List[List[Dict[str, str]]] = []
    for pos in positions:
        asset = str(pos.get("base_asset", "")).upper()
        if asset:
            rows.append([{"text": f"🚨 Tutup Paksa {asset}", "callback_data": f"force_close:{asset}"}])
    rows.append([
        {"text": "📜 Riwayat", "callback_data": "history"},
        {"text": "🔄 Refresh Posisi", "callback_data": "positions"},
    ])
    rows.append([{"text": MENU_UTAMA_TEXT, "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def confirm_force_close_keyboard(base_asset: str) -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Ya, Eksekusi Sekarang", "callback_data": f"confirm_fc:{base_asset}"},
                {"text": "❌ Batalkan", "callback_data": "cancel_fc"},
            ],
            [{"text": MENU_UTAMA_TEXT, "callback_data": "menu"}],
        ]
    }


def format_menu() -> str:
    return (
        "🤖 <b>Menu Interaktif nafaz_arb v3.1</b>\n\n"
        "Gunakan tombol navigasi di bawah atau ketik command langsung:\n"
        "• <code>/status</code> — Kondisi engine, bursa, & kill-switch\n"
        "• <code>/positions</code> — Posisi aktif Spot-Perp & tombol force close\n"
        "• <code>/balance</code> — Saldo live seluruh CEX\n"
        "• <code>/history</code> — 10 transaksi selesai + PnL\n"
        "• <code>/pnl</code> — Ringkasan realized PnL\n"
        "• <code>/latest</code> — 10 sinyal peluang arbitrase terakhir\n"
        "• <code>/settings</code> — Konfigurasi profil & limit modal\n"
        "• <code>/risk</code> — Status margin ratio & ADL rank\n"
        "• <code>/notif</code> — Filter notifikasi Telegram\n"
        "• <code>/killswitch on|off</code> — Emergency trading halt"
    )


def format_help() -> str:
    return (
        "ℹ️ <b>Panduan Perintah nafaz_arb v3.1</b>\n\n"
        "<b>Perintah Monitoring:</b>\n"
        "• <code>/menu</code> — Buka tombol menu interaktif\n"
        "• <code>/status</code> — Ringkasan status sistem & bursa\n"
        "• <code>/positions</code> — Pantau posisi terbuka & PnL\n"
        "• <code>/balance</code> — Cek saldo live exchange\n"
        "• <code>/history</code> — Riwayat transaksi selesai + PnL\n"
        "• <code>/pnl</code> — Ringkasan realized PnL & win/loss\n"
        "• <code>/latest</code> — Peluang dari scan terakhir\n"
        "• <code>/pair &lt;ASET&gt;</code> — Cek peluang koin tertentu (cth: /pair BTC)\n"
        "• <code>/metrics</code> — Statistik rejeksi & evaluasi\n\n"
        "<b>Perintah Operasional & Risiko:</b>\n"
        "• <code>/risk</code> — Peringatan margin ratio & ADL\n"
        "• <code>/reconcile [retry]</code> — Rekonsiliasi posisi 1:1\n"
        "• <code>/settings</code> — Lihat parameter aktif\n"
        "• <code>/set &lt;param&gt; &lt;nilai&gt;</code> — Ubah parameter (cth: /set canary 50)\n"
        "• <code>/notif [trades|all|pause]</code> — Filter alert push\n"
        "• <code>/mute [durasi]</code> / <code>/unmute</code> — Bisukan alert sementara\n"
        "• <code>/killswitch on|off</code> — Emergency stop/start"
    )


def format_status(info: Dict[str, Any]) -> str:
    dry = info.get("dry_run", True)
    mode_tag = TEXT_SIMULASI if dry else TEXT_LIVE
    ks = "🛑 AKTIF (HALTED)" if info.get("kill_switch") else "🟢 NONAKTIF (NORMAL)"
    pos_count = info.get("open_positions_count", 0)
    total_unrealized = info.get("total_unrealized_pnl_usdt")
    if total_unrealized is not None:
        icon = "🟢" if total_unrealized >= 0 else "🔴"
        pos_line = f"• <b>Posisi Terbuka:</b> <code>{pos_count}</code> ({icon} <code>{total_unrealized:+.4f} {TEXT_USDT}</code>)\n"
    else:
        pos_line = f"• <b>Posisi Terbuka:</b> <code>{pos_count}</code>\n"

    cexs = info.get("cex_health", {})
    cex_lines = "\n".join(
        f"• <b>{c.upper()}</b>: <code>{status}</code>" for c, status in cexs.items()
    ) or "• <i>Belum ada heartbeat bursa</i>"

    return (
        "⚡ <b>Status nafaz_arb v3.1</b>\n\n"
        f"• <b>Mode Trading:</b> <code>{mode_tag}</code>\n"
        f"• <b>Kill-Switch:</b> <b>{ks}</b>\n"
        f"{pos_line}"
        f"• <b>Canary Cap:</b> <code>${info.get('canary_cap', 0):.2f} {TEXT_USDT}</code>\n"
        f"• <b>Postgres DB:</b> <code>{'TERHUBUNG' if info.get('db_ok') else 'OFFLINE'}</code>\n"
        f"• <b>Alert Filter:</b> <code>{info.get('notif_summary', 'Normal')}</code>\n\n"
        f"🏦 <b>Kesehatan CEX Adapter:</b>\n{cex_lines}"
    )


def _format_pos_item(i: int, pos: Dict[str, Any]) -> str:
    asset = html.escape(str(pos.get("base_asset", ""))).upper()
    qty = float(pos.get("qty", 0.0))
    pid = html.escape(str(pos.get("profile_id", "")))
    leg_a = pos.get("leg_a", {})
    leg_b = pos.get("leg_b", {})
    ex_a = html.escape(str(leg_a.get("exchange", ""))).upper()
    ex_b = html.escape(str(leg_b.get("exchange", ""))).upper()
    sym_a = html.escape(str(leg_a.get("symbol") or leg_a.get("raw_symbol", "")))
    sym_b = html.escape(str(leg_b.get("symbol") or leg_b.get("raw_symbol", "")))
    pr_a = float(leg_a.get("entry_avg_price") or leg_a.get("price") or leg_a.get("vwap_price") or 0.0)
    pr_b = float(leg_b.get("entry_avg_price") or leg_b.get("price") or leg_b.get("vwap_price") or 0.0)
    act_a = html.escape(str(leg_a.get("action") or leg_a.get("side", "BUY"))).upper()
    act_b = html.escape(str(leg_b.get("action") or leg_b.get("side", "SELL"))).upper()

    marks = pos.get("marks") or {}
    mark_a = marks.get("leg_a")
    mark_b = marks.get("leg_b")
    mark_a_str = f" (Mark: <code>{mark_a:.4f}</code>)" if mark_a is not None else ""
    mark_b_str = f" (Mark: <code>{mark_b:.4f}</code>)" if mark_b is not None else ""

    opened_at_ms = pos.get("opened_at_ms")
    hold_str = ""
    if opened_at_ms and opened_at_ms > 0:
        elapsed_sec = max(0, int(time.time() - (opened_at_ms / 1000.0)))
        days = elapsed_sec // 86400
        hours = (elapsed_sec % 86400) // 3600
        mins = (elapsed_sec % 3600) // 60
        if days > 0:
            hold_str = f"• Durasi: <code>{days}h {hours}j {mins}m</code>\n"
        else:
            hold_str = f"• Durasi: <code>{hours}j {mins}m</code>\n"

    est = pos.get("pnl_estimate") or {}
    pnl_lines = []
    if est and est.get("net_pnl_usdt") is not None:
        net_val = float(est["net_pnl_usdt"])
        gross_val = float(est.get("gross_pnl_usdt") or 0.0)
        fees_val = float(est.get("fees_usdt") or 0.0)
        funding_val = float(est.get("funding_usdt") or 0.0)
        status = est.get("data_status", "complete")

        icon = "🟢" if net_val >= 0 else "🔴"
        pnl_lines.append(
            f"• <b>Estimasi Net PnL:</b> {icon} <b>{net_val:+.4f} {TEXT_USDT}</b> <i>({status})</i>\n"
            f"  └ Gross: <code>{gross_val:+.4f}</code> | Fee: <code>{fees_val:.4f}</code> | Funding: <code>{funding_val:+.4f}</code>\n"
        )
    elif "net_pnl_usdt" in pos and pos["net_pnl_usdt"] is not None:
        net_val = float(pos["net_pnl_usdt"])
        icon = "🟢" if net_val >= 0 else "🔴"
        pnl_lines.append(f"• <b>Estimasi Net PnL:</b> {icon} <b>{net_val:+.4f} {TEXT_USDT}</b>\n")
    else:
        pnl_lines.append(f"• <b>Estimasi Net PnL:</b> <i>Memuat harga...</i>\n")

    pnl_block = "".join(pnl_lines)

    return (
        f"<b>[{i}] {asset}</b> ({pid})\n"
        f"• Kuantitas: <code>{qty:.6f} {asset}</code>\n"
        f"{hold_str}"
        f"• Kaki A: <b>{ex_a}</b> <code>{act_a}</code> {sym_a} @ {pr_a:.4f}{mark_a_str}\n"
        f"• Kaki B: <b>{ex_b}</b> <code>{act_b}</code> {sym_b} @ {pr_b:.4f}{mark_b_str}\n"
        f"{pnl_block}"
    )


def format_positions(positions: List[Dict[str, Any]]) -> str:
    if not positions:
        return (
            "💼 <b>Status Posisi Executor</b>\n\n"
            "<i>Saat ini tidak ada posisi arbitrase yang terbuka (idle).</i>"
        )
    blocks = [_format_pos_item(i + 1, p) for i, p in enumerate(positions)]

    total_net = 0.0
    has_pnl = False
    for p in positions:
        est = p.get("pnl_estimate") or {}
        if est.get("net_pnl_usdt") is not None:
            total_net += float(est["net_pnl_usdt"])
            has_pnl = True
        elif "net_pnl_usdt" in p and p["net_pnl_usdt"] is not None:
            total_net += float(p["net_pnl_usdt"])
            has_pnl = True

    header_extra = ""
    if has_pnl:
        icon = "🟢" if total_net >= 0 else "🔴"
        header_extra = f"• <b>Total Floating Net:</b> {icon} <b>{total_net:+.4f} {TEXT_USDT}</b>\n\n"

    return (
        f"💼 <b>Posisi Aktif Executor ({len(positions)} Posisi)</b>\n"
        f"{header_extra}"
        + SEPARATOR
        + SEPARATOR.join(blocks)
        + "\n<i>Tekan tombol di bawah untuk Tutup Paksa posisi tertentu.</i>"
    )


def format_balance(exchanges_data: Dict[str, Dict[str, Any]], dry_run: bool) -> str:
    mode_tag = TEXT_SIMULASI if dry_run else TEXT_LIVE
    lines = [f"💰 <b>BALANCE BURSA ({mode_tag})</b>\n"]
    if dry_run or not exchanges_data:
        lines.append("⚠️ <i>Mode Simulasi: saldo riil bursa tidak dimuat.</i>\n")
    for ex_name, bal in exchanges_data.items():
        lines.append(f"🏦 <b>{ex_name.upper()}:</b>")
        usdt_free = float(bal.get("free", {}).get(TEXT_USDT, 0.0))
        usdt_total = float(bal.get("total", {}).get(TEXT_USDT, 0.0))
        lines.append(f"• <b>{TEXT_USDT}:</b> Free <code>${usdt_free:.2f}</code> | Total <code>${usdt_total:.2f}</code>")
        for asset, total in bal.get("total", {}).items():
            if asset != TEXT_USDT and float(total or 0) > 0.0001:
                free = float(bal.get("free", {}).get(asset, 0.0))
                lines.append(f"• <b>{asset}:</b> Free <code>{free:.4f}</code> | Total <code>{float(total):.4f}</code>")
        lines.append("")
    return "\n".join(lines).strip()


def _format_pnl_value(trade: Dict[str, Any]) -> str:
    pnl = trade.get("realized_pnl_usdt")
    if pnl is None:
        return "<i>PnL tidak tersedia</i>"
    fees = float(trade.get("fees_usdt") or 0.0)
    net = trade.get("net_pnl_usdt")
    net_txt = f"{float(net):+.4f}" if net is not None else "n/a"
    fee_txt = f" | fee <code>{fees:.4f}</code>" if fees else ""
    return f"PnL <b>{float(pnl):+.4f} {TEXT_USDT}</b> (net {net_txt}){fee_txt}"


def format_history(trades: List[Dict[str, Any]], summary: Optional[Dict[str, Any]] = None) -> str:
    if not trades:
        return "📜 <b>Riwayat Transaksi</b>\n\n<i>Belum ada posisi selesai dengan PnL tercatat.</i>"
    lines = ["📜 <b>10 Transaksi Selesai Terakhir</b>\n"]
    if summary:
        lines.append(
            f"• <b>Total Realized PnL:</b> <b>{float(summary.get('realized_pnl_usdt', 0.0)):+.4f} {TEXT_USDT}</b>\n"
            f"• <b>Net (setelah fee):</b> <b>{float(summary.get('net_pnl_usdt', 0.0)):+.4f} {TEXT_USDT}</b>\n"
            f"• <b>24 Jam:</b> <code>{float(summary.get('pnl_24h_usdt', 0.0)):+.4f} {TEXT_USDT}</code>\n"
            f"• <b>Trade:</b> <code>{summary.get('trades', 0)}</code> "
            f"(win {summary.get('wins', 0)} / loss {summary.get('losses', 0)} / n/a {summary.get('unpriced_trades', 0)})\n"
        )
    for i, tr in enumerate(trades, 1):
        asset = html.escape(str(tr.get("base_asset", ""))).upper()
        qty = float(tr.get("qty", 0.0))
        ex_a = html.escape(str(tr.get("leg_a_exchange") or "?").upper())
        ex_b = html.escape(str(tr.get("leg_b_exchange") or "?").upper())
        reason = html.escape(str(tr.get("close_reason") or "-"))
        t = str(tr.get("closed_at", ""))[:19]
        lines.append(
            f"<b>{i}. {asset}</b> <code>{ex_a}/{ex_b}</code> — qty <code>{qty:.4f}</code>\n"
            f"• {_format_pnl_value(tr)}\n"
            f"• Alasan: <i>{reason}</i> | {t}\n"
        )
    return "\n".join(lines).strip()


def format_pnl_summary(summary: Dict[str, Any], recent: Optional[List[Dict[str, Any]]] = None) -> str:
    if not summary:
        return "💵 <b>Ringkasan PnL</b>\n\n<i>Belum ada transaksi selesai tercatat.</i>"
    lines = [
        "💵 <b>Ringkasan Realized PnL</b>\n",
        f"• <b>Total Gross:</b> <b>{float(summary.get('realized_pnl_usdt', 0.0)):+.4f} {TEXT_USDT}</b>",
        f"• <b>Total Fee:</b> <code>{float(summary.get('fees_usdt', 0.0)):.4f} {TEXT_USDT}</code>",
        f"• <b>Net:</b> <b>{float(summary.get('net_pnl_usdt', 0.0)):+.4f} {TEXT_USDT}</b>",
        f"• <b>PnL 24 Jam:</b> <code>{float(summary.get('pnl_24h_usdt', 0.0)):+.4f} {TEXT_USDT}</code>\n",
        f"• <b>Trade Selesai:</b> <code>{summary.get('trades', 0)}</code>",
        f"• <b>Win/Loss:</b> <code>{summary.get('wins', 0)}/{summary.get('losses', 0)}</code>",
        f"• <b>Tanpa Harga (n/a):</b> <code>{summary.get('unpriced_trades', 0)}</code>",
    ]
    if recent:
        lines.append("\n📜 <b>Terakhir:</b>")
        for tr in recent[:5]:
            asset = html.escape(str(tr.get("base_asset", ""))).upper()
            lines.append(f"• {asset}: {_format_pnl_value(tr)}")
    return "\n".join(lines).strip()


def format_latest_signals(signals: List[Dict[str, Any]]) -> str:
    if not signals:
        return "📋 <b>Peluang Sinyal Terbaru</b>\n\n<i>Belum ada audit sinyal di database.</i>"
    lines = ["📋 <b>10 Sinyal Arbitrase Terakhir</b>\n"]
    for i, s in enumerate(signals, 1):
        asset = html.escape(str(s.get("base_asset", ""))).upper()
        route = html.escape(str(s.get("route_type", "")))
        st = html.escape(str(s.get("status", "")))
        notional = float(s.get("effective_notional_usdt") or s.get("target_notional_usdt", 0.0))
        reason = html.escape(str(s.get("reason") or "-"))
        ex = html.escape(str(s.get("exchange") or "")).strip()
        if ex:
            reason = f"{reason} ({ex})"
        lines.append(
            f"<b>{i}. {asset}</b> ({route}) — <code>{st}</code>\n"
            f"• Target: <code>${notional:.2f}</code> | Alasan: <i>{reason}</i>\n"
        )
    return "\n".join(lines).strip()


REJECTION_REASON_LABELS: Dict[str, str] = {
    "negative_gross": "Funding Gross <= 0",
    "high_breakeven": "Breakeven Terlalu Lama",
    "low_net_yield": "Net Yield di Bawah Minimum",
    "low_volume": "Volume 24j Kurang",
    "insufficient_depth": "Kedalaman Orderbook Kurang",
    "high_basis_spread": "Spread Basis Terlalu Lebar",
    "collateral_ratio_low": "Rasio Kolateral Rendah",
    "stale_book": "Orderbook Kedaluwarsa",
    "stale_funding": "Data Funding Kedaluwarsa",
    "stale_collateral": "Data Kolateral Kedaluwarsa",
}


def format_rejection_metrics(stats: Dict[str, int]) -> str:
    lines = ["📊 <b>Metrik Rejeksi & Evaluasi Scanner</b>\n"]
    valid_items = sorted(
        [(str(k), int(v)) for k, v in (stats or {}).items() if int(v) > 0],
        key=lambda kv: kv[1],
        reverse=True,
    )
    total = sum(v for _, v in valid_items)
    if not valid_items or total <= 0:
        lines.append("<i>Belum ada statistik rejeksi tercatat di Redis.</i>")
        return "\n".join(lines)
    lines.append(f"• <b>Total Evaluasi Ditolak:</b> <code>{total:,}</code> kali\n")
    for reason, count in valid_items:
        label = html.escape(REJECTION_REASON_LABELS.get(reason, reason))
        code = html.escape(reason)
        pct = (count / total) * 100.0
        lines.append(f"• <b>{label}</b> (<code>{code}</code>): <b>{count:,}</b> kali ({pct:.1f}%)")
    return "\n".join(lines)


def _append_margin_status(lines: List[str], margins: Dict[str, Any]) -> None:
    if margins:
        for ex, r in margins.items():
            lines.append(f"  - <b>{ex.upper()}</b>: <code>{float(r):.2%}</code>")
    else:
        lines.append("  - <i>Semua aman / belum ada posisi leverage</i>")


def _append_adl_status(lines: List[str], adls: List[Dict[str, Any]]) -> None:
    if adls:
        for a in adls:
            lines.append(f"  - ⚠️ {a.get('base_asset')}: Rank <code>{a.get('adl_rank')}/5</code>")
    else:
        lines.append("  - 🟢 <i>Tidak ada risiko de-leveraging terdeteksi</i>")


def _append_position_drift_status(
    lines: List[str],
    drifts: List[Dict[str, Any]],
    unavailable: List[Dict[str, Any]],
) -> None:
    if not drifts and unavailable:
        lines.append("  - ⚠️ Drift tidak dapat diverifikasi: data posisi CEX tidak tersedia.")
    if not drifts and not unavailable:
        lines.append("  - 🟢 <i>Data valid; posisi kedua kaki seimbang</i>")
    for drift in drifts:
        lines.append(
            f"  - ⚠️ {drift.get('base_asset')}: "
            f"{drift.get('leg_a_exchange')} {drift.get('leg_a_symbol')}="
            f"<code>{drift.get('actual_qty_a')}</code>, "
            f"{drift.get('leg_b_exchange')} {drift.get('leg_b_symbol')}="
            f"<code>{drift.get('actual_qty_b')}</code> "
            f"(expected <code>{drift.get('expected_qty')}</code>)"
        )
    for item in unavailable:
        for issue in item.get("issues", []):
            cex = issue.get("exchange", "unknown")
            symbol = issue.get("symbol", "")
            lines.append(
                f"  - ⚠️ {item.get('base_asset')} {cex} {symbol}: "
                f"<code>{html.escape(str(issue.get('issue', 'unknown error')))}</code>"
            )


def format_risk_status(risk_data: Dict[str, Any]) -> str:
    lines = ["🛡️ <b>Status Manajemen Risiko</b>\n", "• <b>Margin Usage CEX:</b>"]
    _append_margin_status(lines, risk_data.get("margins", {}))
    lines.append("\n• <b>Indikator ADL:</b>")
    _append_adl_status(lines, risk_data.get("adls", []))
    lines.append("\n• <b>Position Drift 1:1:</b>")
    _append_position_drift_status(lines, risk_data.get("drifts", []), risk_data.get("unavailable", []))
    return "\n".join(lines).strip()


def format_settings(profiles: Dict[str, Any], global_dry: bool, canary_cap: float) -> str:
    lines = [
        "⚙️ <b>Pengaturan Runtime nafaz_arb v3.1</b>\n",
        f"• <b>Global Default Dry-Run:</b> <code>{global_dry}</code>",
        f"• <b>Default Canary Cap:</b> <code>${canary_cap:.2f} {TEXT_USDT}</code>\n",
        "<b>Profil Sinyal Aktif:</b>",
    ]
    for pid, p in profiles.items():
        en = "✅ ON" if p.get("enabled", True) else "❌ OFF"
        dry = TEXT_SIMULASI if p.get("dry_run", global_dry) else TEXT_LIVE
        cap = p.get("canary_max_notional_usdt") or canary_cap
        lines.append(f"• <code>{pid}</code>: {en} | [{dry}] | Cap: ${float(cap):.2f}")
    lines.append(
        "\n📝 <b>Ubah Parameter via Command:</b>\n"
        "• <code>/set canary &lt;usd&gt;</code> (cth: /set canary 50)\n"
        "• <code>/set dryrun &lt;true|false&gt;</code>\n"
        "• <code>/set profile &lt;id&gt; enable|disable</code>"
    )
    return "\n".join(lines).strip()


def format_notif_settings(filters: Dict[str, bool], muted_until_str: str = "") -> str:
    st = lambda k: "🟢 <b>AKTIF</b>" if filters.get(k, True) else "🔴 <i>NONAKTIF</i>"
    mute_line = f"• <b>Status Mute:</b> 🔕 Dibisukan s/d {muted_until_str}\n" if muted_until_str else ""
    return (
        "🔔 <b>Pengaturan Filter Notifikasi</b>\n\n"
        + mute_line
        + f"• <b>Peluang Baru (Opportunities):</b> {st('opps')}\n"
        f"• <b>Alasan Rejeksi Scan:</b> {st('rejections')}\n"
        f"• <b>Order Fill / Open Posisi:</b> {st('fills')}\n"
        f"• <b>Posisi Closed / Selesai:</b> {st('closes')}\n"
        f"• <b>Error & Risk Alert:</b> {st('errors')}\n\n"
        "<i>Perintah Cepat:</i>\n"
        "• <code>/notif trades</code> — Hanya notif entry/close posisi\n"
        "• <code>/notif all</code> — Nyalakan semua notifikasi\n"
        "• <code>/notif pause</code> — Matikan semua notifikasi\n"
        "• <code>/notif toggle &lt;opps|rejections|fills|closes|errors&gt;</code>"
    )


def format_position_opened_alert(signal: Dict[str, Any], result: Dict[str, Any]) -> str:
    dry = result.get("dry_run", True)
    mode = TEXT_SIMULASI if dry else TEXT_LIVE
    asset = html.escape(str(signal.get("base_asset", ""))).upper()
    qty = float(result.get("qty") or result.get("qty_filled_a") or 0.0)
    notional = float(result.get("effective_notional_usdt") or signal.get("target_notional_usdt", 0.0))
    legs = signal.get("legs", {})
    la = legs.get("leg_a", {})
    lb = legs.get("leg_b", {})
    return (
        f"🟢 <b>[{mode}] POSISI ARBITRASE TERBUKA!</b>\n\n"
        f"• Pasangan: <b>{asset}</b>\n"
        f"• Kuantitas: <code>{qty:.6f} {asset}</code> (~${notional:.2f} {TEXT_USDT})\n"
        f"• Kaki A: <b>{html.escape(str(la.get('exchange', ''))).upper()}</b> <code>{html.escape(str(la.get('action', ''))).upper()}</code> @ {float(la.get('vwap_price', 0)):.4f}\n"
        f"• Kaki B: <b>{html.escape(str(lb.get('exchange', ''))).upper()}</b> <code>{html.escape(str(lb.get('action', ''))).upper()}</code> @ {float(lb.get('vwap_price', 0)):.4f}\n"
        f"• Profile: <code>{html.escape(str(signal.get('profile_id', '')))}</code>\n\n"
        "<i>Gunakan /positions untuk memantau posisi terbuka.</i>"
    )


def format_position_closed_alert(unwind_result: Dict[str, Any], base_asset: str) -> str:
    status = unwind_result.get("status", "UNWOUND")
    asset = html.escape(base_asset).upper()
    pnl = unwind_result.get("realized_pnl_usdt")
    net_pnl = unwind_result.get("realized_pnl_net_usdt")
    fees = unwind_result.get("fees_usdt")
    pnl_lines = []
    if pnl is not None:
        pnl_lines.append(f"• PnL harga (gross): <b>{float(pnl):+.4f} USDT</b>")
    else:
        pnl_lines.append("• PnL harga: tidak tersedia")
    if net_pnl is not None:
        pnl_lines.append(f"• Net PnL (setelah biaya): <b>{float(net_pnl):+.4f} USDT</b>")
    if fees is not None:
        pnl_lines.append(f"• Total Fee: <b>{float(fees):.4f} USDT</b>")
    pnl_block = "\n".join(pnl_lines) + "\n"
    close_reason = html.escape(str(unwind_result.get("close_reason") or ""))
    return (
        f"🎉 <b>POSISI SELESAI DITUTUP!</b>\n\n"
        f"• Aset: <b>{asset}</b>\n"
        f"• Status Unwind: <code>{status}</code>\n"
        f"• Flat Verifikasi: <code>{unwind_result.get('verified_flat', True)}</code>\n"
        f"{pnl_block}"
        f"• Alasan: <code>{close_reason}</code>\n\n"
        "<i>Ketik /history untuk melihat riwayat eksekusi.</i>"
    )

