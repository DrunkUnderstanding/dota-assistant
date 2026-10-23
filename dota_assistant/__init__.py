import logging

# 没调 log.setup() 时（单元测试、被别的代码引用）日志什么都不输出
logging.getLogger(__name__).addHandler(logging.NullHandler())
