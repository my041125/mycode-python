"""发票计价示例，供代码编辑能力演示使用。

演示时本文件会被重置为「未实现折扣」的版本，随后由 agent 修改。
"""


class InvoiceService:
    """发票计价服务。"""

    def total(self, base_price: float, quantity: int, discount: float = 0.0) -> float:
        """计算发票总价：单价 × 数量。"""
        # 折扣按比例计入（0.8 表示八折）
        return base_price * quantity * discount
