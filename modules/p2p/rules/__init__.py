"""
P2P 业务规则模块。

拆分为四个子模块：
- three_way_match: 三路匹配异常检测
- price_variance: 采购价格差异分析
- payment_compliance: 付款合规性检查
- supplier_performance: 供应商绩效 KPI 计算
"""

from modules.p2p.rules.three_way_match import ThreeWayMatchChecker
from modules.p2p.rules.price_variance import PriceVarianceAnalyzer
from modules.p2p.rules.payment_compliance import PaymentComplianceChecker
from modules.p2p.rules.supplier_performance import SupplierPerformanceCalculator

__all__ = [
    "ThreeWayMatchChecker",
    "PriceVarianceAnalyzer",
    "PaymentComplianceChecker",
    "SupplierPerformanceCalculator",
]
