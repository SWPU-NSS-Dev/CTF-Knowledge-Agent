# 技术说明

`malloc@plt` resolves through `R_X86_64_JUMP_SLOT`.

栈溢出与任意地址写。

```c
free(__free_hook);
```
