"""Shared semantic obligations for existing LLM stages, not a new classifier."""
FINANCIAL_REVIEW_VERSION = 'financial-evidence/6'
FINANCIAL_REVIEW_CONTRACT = '''
金融證據契約 financial-evidence/6：這些是語義核對責任，不是以字詞命中判錯。
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
風控結論須區分來源明載事實、程式計算、情境假設、使用者明示政策、當下投資決策與普遍必然性主張，並檢查結論範圍是否超出已證明的前提。選擇 WAIT 或 0% 部位可以是特定時點與風險偏好下的合理決策，但不等於證明所有非零部位都不可控或不存在其他可行方案。ATR、波動度或壓力情境可支持風險大小、停損距離與情境敏感度的分析，單獨不足以證明任何非零部位都不可能符合風險限制。若結論是「依本策略不得持倉」，須有實際明示的風險政策、風險預算、交易限制或足以推出該結論的前提，並將結論限定在該政策與假設內。不得替使用者發明最大部位、風險預算、事件禁令或 ATR 門檻；沒有明示時保留為未知。壓力測試與跳空風險是條件情境，不是必然結果；不得假設風險為零。措辭差異或同一組事實下存在多個合理政策選擇，不構成 finding；只有結論超出證據或明示約束時，沿用既有 material finding／clarity_only 分流。
預測期間與交易持有期間須分開核對：長期目標價不能直接充當短期交易目標、到期前報酬或同一期間的風險報酬比；跨期間使用須明示時間橋接、催化事件、估值與退出假設，無依據時只能作背景或情境。交易有效期限、預計持有期、來源預測期間未知時不得補猜，也不得僅因引用長期預測就否決明確限定為背景的歷史分析。
停損觸發價不是保證成交價；停損單、限價停損單、成交假設、滑價、流動性、跳空、盤中或盤外條件須分開說明。依觸發價計算的部位風險或帳戶損失只在明示成交假設下成立，不能直接稱為實際最大損失、保本或硬上限。
歷史樣本不是未來最大損失的硬上限；歷史跌幅乘部位權重只代表該情境下、該分母與槓桿假設的帳戶損失，不是尾端風險保證、使用者可接受的風險預算或已證明能遵守的政策。不得從情境數字反推使用者政策；缺少實際明示的政策時，將部位與損失限定為分析者假設。
目前財務或交易結論須核對截至 as_of 已發布且與命題相關的年度與期中資料，分開 received（來源或證據已收到）與 structured（同期間、版本已有結構化數值）。新聞提到最新報表不代表財務 Fact 已涵蓋；年度資料不能自動替代最新期中報表，單季、累計 YTD、滾動 TTM 不能互換。依明示 publication、period、coverage 與來源引用判斷；不得從年份、抓取日、公司所在地或文句猜最新應有報表或強制套用某國表單。未知可用性須說未知，已知本次未提供須說本次未提供。financial_coverage 診斷僅描述明示 metadata；WARN 不等於自動否決或語義合格，仍須在既有審查中核對用途。歷史分析仍可使用舊報表，須限定期間與用途；明示 financial_coverage_requirements 才構成對應的收件及結構化門檻，不得自行發明強制最新報表政策。
'''.strip()

from .trade_evidence import TRADE_EVIDENCE_INSTRUCTIONS
FINANCIAL_REVIEW_CONTRACT += '\n' + TRADE_EVIDENCE_INSTRUCTIONS


def ingestion_diagnostics(run):
    """Typed adapter diagnostics, including explicit coverage gaps."""
    from copy import deepcopy
    from .financial_coverage import financial_coverage_diagnostics
    return [deepcopy(rec.meta[key]) for rec in getattr(run, 'records', [])
            for key in ('cash_flow_extraction', 'financial_statement_extraction',
                        'financial_data_extraction', 'market_data_extraction')
            if getattr(rec, 'meta', {}).get(key)] + financial_coverage_diagnostics(run)
