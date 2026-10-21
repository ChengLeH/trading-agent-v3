"""Shared semantic obligations for existing LLM stages, not a new classifier."""
FINANCIAL_REVIEW_VERSION = 'financial-evidence/4'
FINANCIAL_REVIEW_CONTRACT = '''
金融證據契約 financial-evidence/4：這些是語義核對責任，不是以字詞命中判錯。
來源原始值、計算值與展示值須區分；assertion.value 保留 canonical 值。
可另附 assertion.presentation={value,unit,decimal_places,rounding:half_up|half_even} 明示換單位與捨入。
合理四捨五入或不同抓取時點造成的小差異，若來源、時間與精度足以解釋，不列重大錯誤；
不得因此放過錯公式、錯主體、錯期間、錯幣別、錯會計基礎或錯分母。
結構驗證的 WARN 不是語義合格：逐一核對 binding 實際用途，未知名稱別名必須讀來源判斷，不能靠相似字串。
對 calculation_definitions 中明示的運算，依其 operation meaning、ordered inputs、meta parameters、output unit、rounding 與 restrictions 核對實際計算；不得從 ID、相近數值或文字猜公式。數值接近不能證明指標、期間或輸入角色相同；未知運算須保留為未知，不補猜公式。
分開來源發布日、事件發生日、共識彙整日與各分析師預測的 as-of 日。
彙整日在事件之後，不足以證明每位分析師都已更新；也不得無證據宣稱共識形成於事件之前或尚未反映事件。
比較需對齊 entity/metric/period/unit/basis/scope；可明示做跨期或跨口徑比較，但不得冒充同一量。
季度 EPS 共識、年度 EPS、未來十二月股價目標是不同指標，不能互相代替作為指引落差證據。
區分歸屬母公司權益、合併權益、非控制權益及可贖回非控制權益；缺少元件不能從殘差捏造。
估值結論須繼承上游盈餘品質、一次性項目與可比性限制。
GAAP P/E 算對不代表已證明便宜；淨利減營業利益不等於可直接剔除的稅後一次性收益，
也不可拿營業利益當淨利計 P/E。FCF/市值/EV 的槓桿、股權與期間口徑亦須相容。
情境壓力測試可以故意極端，須標清假設、時點及固定分母；不得將其當作基準預測或既成事實。
資金用途、收款主體、交割時點、可動用現金與已承諾融資須按來源區分；
出售款不一定歸買方可用，不可自動扣抵價金，不得憑空指定現金／舉債比例。
目前提供的證據沒有資料，只能說本次證據未提供；不能推成公司沒有披露或事件沒有發生。
缺資料時縮小主張或標 evidence_needed，讓主 Agent 補入可追溯來源；不得編數或刪真正因果依賴。
判定 evidence_needed 時，須附 typed evidence_request 說明待確認命題及已知資料維度；未知維度留空，不從 finding 文字猜來源 ID。
相同事實、限制與情境假設在下游若被轉成確定結論，須列 material finding；只影響行文且不改意思的提醒列 clarity_only。
來源沿革是明示的 provenance 註記，不是程式認證的來源獨立性。逐一核對來源正文及其明示的 origin_id、derived_from、publisher 與證據引用；同一原始文件的轉載不得當成多份獨立佐證。不同網址、發布者或平台也不自動代表獨立；相同事件不代表同一來源。缺少沿革註記時獨立性為未知，不能推定為獨立或因此否決一項由單一來源支持的事實。只有 Claim 實際依賴「獨立佐證」時，才判斷來源能否支持該語義；來源數量本身不構成證據充分性門檻。
'''.strip()


def ingestion_diagnostics(run):
    """Typed adapter diagnostics, including explicit coverage gaps."""
    from copy import deepcopy
    return [deepcopy(rec.meta[key]) for rec in getattr(run, 'records', [])
            for key in ('cash_flow_extraction', 'financial_statement_extraction',
                        'financial_data_extraction', 'market_data_extraction')
            if getattr(rec, 'meta', {}).get(key)]
