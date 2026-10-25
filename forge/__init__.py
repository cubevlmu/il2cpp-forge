"""il2cpp-forge -- AI-native, evidence-gated reconstruction of Unity IL2CPP assemblies.

The package is deliberately dependency free: every stage runs on the standard
library plus whatever external tools the user already has (Il2CppDumper,
Cpp2IL, Ghidra/IDA export, dotnet).  That keeps it usable inside agent
sandboxes where installing wheels is not an option.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
