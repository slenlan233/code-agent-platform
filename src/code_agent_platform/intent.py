"""Resolve explicit approval sequencing without granting execution permission."""
import re


def requests_database_deletion(text: str) -> bool:
    text = re.sub(r'\s+', ' ', text).lower()
    for clause in re.split(r'[。；;，,\n]|但是|但', text):
        clause = clause.replace('可以帮我', '帮我')
        if re.search(r'(?:不要|不能|不准|禁止|不需要|保留).{0,12}(?:删|数据库)|(?:可以|能不能|是否|如何|怎么).{0,8}(?:删|drop)|(?:do not|don.t|how to) .{0,10}(?:drop|delete)', clause):
            continue
        if re.search(r'数据库(?:中|里|内|的).{0,10}(?:表|数据|记录)', clause):
            continue
        if re.search(r'数据库(?:表|的.{0,8}(?:连接|配置|账号))', clause):
            continue
        if re.search(r'(?:删除|删掉|删去).{0,20}(?:数据库|业务库)|(?:数据库|业务库).{0,6}(?:删除|删掉|删了|删去)|(?:drop|delete) (?:the |this |current |project |business )*(?:database|db)\b', clause):
            return True
    return False


def requests_project_reset(text: str) -> bool:
    text = re.sub(r'\s+', ' ', text).lower()
    if re.search(r'(?:不要|不准|禁止|暂不).{0,8}(?:清空|重置|删除)|(?:do not|don.t) (?:clear|reset|delete)', text):
        return False
    patterns = (
        r'(?:清空|重置).{0,8}(?:项目|文件夹|目录)',
        r'(?:项目|文件夹|目录).{0,18}(?:所有|全部|一切).{0,6}(?:删掉|删除|清空|移除)',
        r'(?:删掉|删除|移除).{0,8}(?:项目|文件夹|目录).{0,8}(?:所有|全部)(?:文件|代码|内容)',
        r'(?:clear|reset|empty) (?:the |this |current |entire )*(?:project|folder|directory)',
        r'delete (?:all|everything) (?:files |code )?(?:in|from) (?:the |this |current )*(?:project|folder|directory)',
    )
    return any(re.search(pattern, text) for pattern in patterns)


def requests_implementation_after_approval(text: str) -> bool:
    # Only repair the model's plan/modify ambiguity. Answer routing and explicit
    # readonly/plan modes are handled separately and are never promoted here.
    text = re.sub(r'\s+', ' ', text).lower()
    if re.search(r'(?:不要|暂不|不需要|无需|禁止).{0,4}(?:执行|实施|开发|写入|修改代码)|(?:do not|don.t|no) (?:implement|execute|write)',text):
        return False
    chinese = r'(?:确认|批准|审批(?:通过)?|同意).{0,16}(?:后|再).{0,16}(?:执行|实施|开始|开发|编码|写入|动手)'
    english = r'(?:after|once|when).{0,40}(?:approv\w*|confirm\w*).{0,60}(?:implement|execute|start|proceed|code|write)'
    return bool(re.search(chinese, text) or re.search(english, text))
