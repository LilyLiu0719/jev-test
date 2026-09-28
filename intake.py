"""
問診題庫與症狀事實表 —— 這是「內容」，不是程式。

中華汽車的維修手冊回答的是「技師拿著診斷電腦在工位上該做什麼」。1,468 個
檢查節點裡有 66% 需要 C.M.U.T. 或萬用表，業務站在車旁邊一題也答不出來。
所以問診題目不能從手冊直接取用，必須另外寫。

這份檔案就是那份另外寫的內容：

  SYMPTOM_FACTS  每個症狀在幾個「業務問得出來」的面向上的取值
  QUESTIONS      對應那些面向的問法，用客戶聽得懂的話

FACTS 的值大多是從 out/symptom_graphs/*.json 的症狀標題推出來的（標題本身
就寫著「ABS 警示燈不會亮起」這種話），少數是人工判讀。每一筆都標了來源，
標成 "authored" 的需要中華技術課確認。

事實值有三種狀態：

  "abs"            只有這個答案相容。客戶答別的就把這個症狀排除。
  ("none", "abs")  這幾個都相容。客戶答清單外的才排除。
  None             不限制。這個面向切不開這個症狀。

第三種（tuple）是後來補的。第一版只有前後兩種，於是「引擎起動後 ABS 燈仍亮」
被填成 function = "none"，意思變成「客戶明確表示功能正常」。但當初填的理由是
「客戶通常不會注意到 ABS 失效」，那是兩回事。實測時模型把「ABS 燈亮」推論成
「ABS 功能故障」，正確的症狀就被這一格排除掉了。
寫成 ("none", "abs") 才是實話：客戶可能說功能正常，也可能說 ABS 不靈，兩種都
算數，但他不會說是斜坡起步輔助的問題。
"""

# --------------------------------------------------------------- 事實面向 ---
# 每個面向是一個業務可以當場問出來的問題。值域刻意小，因為客戶只會給模糊答案。

FACT_KEYS = ["light", "light_state", "function", "function_issue", "noise", "feel"]

FACT_VALUES = {
    "light":          ["abs", "brake", "asc", "asc_off", "none"],
    "light_state":    ["stays_on", "never_lights"],
    "function":       ["abs", "asc", "hsa", "ess", "steering", "none"],
    "function_issue": ["inactive", "over_active", "wrong_context", "cannot_disable"],
    "noise":          [True, False],
    "feel":           [True, False],
}


# ------------------------------------------------------------- 症狀事實表 ---
# src: title  = 直接讀症狀標題得到
#      authored = 人工判讀，需要技術課確認

SYMPTOM_FACTS = {
    "G35C_SYM1": {   # C.M.U.T.-II 無法與 ABS/ASC 系統通訊
        "facts": {"light": None, "light_state": None, "function": None,
                  "function_issue": None, "noise": None, "feel": None},
        "needs_tool": True,   # 業務問不出來，只有接上診斷電腦才知道
        "src": "title",
    },
    "G35C_SYM2": {   # 手煞車已釋放，煞車警示燈仍 ON（ABS 燈 OFF）
        "facts": {"light": "brake", "light_state": "stays_on", "function": "none",
                  "function_issue": None, "noise": None, "feel": None},
        "src": "title",
    },
    "G35C_SYM3": {   # 點火 ON（引擎熄火）後 ABS 警示燈不會亮起
        "facts": {"light": "abs", "light_state": "never_lights", "function": "none",
                  "function_issue": None, "noise": None, "feel": None},
        "src": "title",
    },
    "G35C_SYM4": {   # 點火 ON（引擎熄火）後煞車警示燈不會亮起
        "facts": {"light": "brake", "light_state": "never_lights", "function": "none",
                  "function_issue": None, "noise": None, "feel": None},
        "src": "title",
    },
    "G35C_SYM5": {   # 引擎起動後 ABS 警示燈仍亮著
        "facts": {"light": "abs", "light_state": "stays_on", "function": ("none", "abs"),
                  "function_issue": None, "noise": None, "feel": None},
        "draft": ["function"],
        # 客戶多半只說燈亮，但急煞過的人可能說 ABS 不靈。兩種都相容。
        "src": "title",
    },
    "G35C_SYM6": {   # 引擎起動後 ASC 警示燈仍 ON
        "facts": {"light": "asc", "light_state": "stays_on", "function": ("none", "asc"),
                  "function_issue": None, "noise": None, "feel": None},
        "draft": ["function"],
        # 同上，可能說功能正常，也可能說 ASC 沒作用。
        "src": "title",
    },
    "G35C_SYM7": {   # 引擎起動後 ASC OFF 指示燈仍 ON
        "facts": {"light": "asc_off", "light_state": "stays_on", "function": ("none", "asc"),
                  "function_issue": None, "noise": None, "feel": None},
        "draft": ["function"],
        # 同上。ASC OFF 燈亮代表穩定控制被關掉，客戶可能有感。
        "src": "title",
    },
    "G35C_SYM8": {   # 按住 ASC OFF 開關 3 秒以上無法解除車身穩定控制
        "facts": {"light": "none", "light_state": None, "function": "asc",
                  "function_issue": "cannot_disable", "noise": None, "feel": None},
        "draft": ["light"],
        # 原因只有開關與線束故障，不會點亮警示燈
        "src": "authored",
    },
    "G35C_SYM9": {   # 煞車作動異常
        "facts": {"light": "none", "light_state": None, "function": ("none", "abs"),
                  "function_issue": None, "noise": None, "feel": True},
        "draft": ["function"],
        # 原因全是液壓／總泵，但客戶可能把煞車不靈歸給 ABS。
        "src": "authored",
    },
    "G35C_SYM10": {  # ASC 不作動或作動不良
        "facts": {"light": ("asc", "none"), "light_state": None, "function": "asc",
                  "function_issue": "inactive", "noise": None, "feel": None},
        "draft": ["light"],
        # 通常會點亮 ASC 燈，但不保證；兩種都相容
        "src": "title",
    },
    "G35C_SYM11": {  # ASC-ECU 電源供應迴路系統
        "facts": {"light": "asc", "light_state": "stays_on", "function": "asc",
                  "function_issue": "inactive", "noise": None, "feel": None},
        "src": "authored",
    },
    "G35C_SYM12": {  # 方向盤感知器電源供應迴路系統
        "facts": {"light": "asc", "light_state": "stays_on", "function": "steering",
                  "function_issue": "inactive", "noise": None, "feel": None},
        "src": "authored",
    },
    "G35C_SYM13": {  # ABS / 車身穩定控制作動太頻繁
        "facts": {"light": "none", "light_state": None, "function": "abs",
                  "function_issue": "over_active", "noise": None, "feel": None},
        "draft": ["light"],
        # 原因全是輪胎、定位、胎壓——系統不認為自己故障
        "src": "title",
    },
    "G35C_SYM14": {  # HSA 不作動
        "facts": {"light": "none", "light_state": None, "function": "hsa",
                  "function_issue": "inactive", "noise": None, "feel": None},
        "draft": ["light"],
        # 把握最低：原因含 CAN 匯流排與 ASC-ECU，那些會亮燈
        "src": "title",
    },
    "G35C_SYM15": {  # HSA 在平地上作動
        "facts": {"light": "none", "light_state": None, "function": "hsa",
                  "function_issue": "wrong_context", "noise": None, "feel": None},
        "draft": ["light"],
        # 誤作動不觸發故障碼，通常不亮燈
        "src": "title",
    },
    "G35C_SYM16": {  # 液壓單元的初始檢查聲音太大聲
        "facts": {"light": "none", "light_state": None, "function": "none",
                  "function_issue": None, "noise": True, "feel": None},
        "src": "title",
    },
    "G35C_SYM17": {  # ESS 不作動或作動異常
        "facts": {"light": "none", "light_state": None, "function": "ess",
                  "function_issue": "inactive", "noise": None, "feel": None},
        "draft": ["light"],
        # ESS 只是煞車燈閃爍，不作動不點亮 ABS／ASC 燈
        "src": "title",
    },
}


# ----------------------------------------------------------------- 題庫 -----
# ask  : 業務對客戶講的話
# probe: 送給模型的判斷指令（從客戶的回答抽出結構化值）
# opts : 選項 -> 客戶聽得懂的說法

# 每個 probe 都接這一句。第一版只有第一題寫了，結果模型把「ABS 燈亮」推論成
# 「ABS 功能有問題」，信心 1.00，正確的症狀反而被這個推論排除掉。抽取跟推論
# 是兩件事，要講明。
_ONLY_STATED = ("只抽客戶明確講出來的，不要從其他描述推論。"
                "客戶沒有直接提到就選 not_stated。")

QUESTIONS = [
    {
        "fact": "light",
        "ask": "儀表板上有亮起哪一個警示燈嗎？",
        "probe": "客戶明確提到儀表板上亮起哪一個警示燈？" + _ONLY_STATED,
        "opts": {
            "abs":     "ABS 警示燈",
            "brake":   "煞車警示燈（驚嘆號）",
            "asc":     "ASC / 車身穩定警示燈",
            "asc_off": "ASC OFF 指示燈",
            "none":    "都沒有亮",
        },
    },
    {
        "fact": "light_state",
        "ask": "那個燈是一直亮著不熄，還是該亮的時候根本不會亮？",
        "probe": "客戶明確描述那個燈是持續亮著，還是該亮時不亮？" + _ONLY_STATED,
        "opts": {
            "stays_on":     "一直亮著不熄",
            "never_lights": "該亮時不會亮",
        },
    },
    {
        "fact": "function",
        "ask": "有哪一項功能覺得不對勁嗎？",
        "probe": "客戶明確抱怨哪一個系統的功能不對？"
                 "警示燈亮不等於客戶抱怨功能，那是兩件事。" + _ONLY_STATED,
        "opts": {
            "abs":      "ABS 防鎖死煞車",
            "asc":      "ASC 車身穩定",
            "hsa":      "斜坡起步輔助",
            "ess":      "緊急煞車警示",
            "steering": "方向盤／轉向相關",
            "none":     "功能都正常，只是燈的問題",
        },
    },
    {
        "fact": "function_issue",
        "ask": "那項功能是完全不作動，還是作動的時機不對？",
        "probe": "客戶明確描述的功能異常屬於哪一種？" + _ONLY_STATED,
        "opts": {
            "inactive":       "完全不作動",
            "over_active":    "作動太頻繁",
            "wrong_context":  "不該作動的時候作動",
            "cannot_disable": "想關掉卻關不掉",
        },
    },
    {
        "fact": "noise",
        "ask": "起步或煞車的時候有聽到不尋常的聲音嗎？",
        "probe": "客戶有沒有明確提到異常聲音？" + _ONLY_STATED,
        "opts": {True: "有聽到異音", False: "沒有異音"},
    },
    {
        "fact": "feel",
        "ask": "踩煞車的感覺有變嗎？例如變硬、變軟、或車子偏一邊？",
        "probe": "客戶有沒有明確描述煞車手感或制動力異常？" + _ONLY_STATED,
        "opts": {True: "煞車手感有異常", False: "煞車手感正常"},
    },
]

QUESTION_BY_FACT = {q["fact"]: q for q in QUESTIONS}

# 業務最多問幾題就該收手把車交給工廠。客戶不是來被審問的。
ASK_BUDGET = 3
