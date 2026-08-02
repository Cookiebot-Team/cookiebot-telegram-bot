from setuptools import setup
from Cython.Build import cythonize

setup(
    name="cy_hot",
    ext_modules=cythonize(
        ["cy/cooldowns.py", "cy/textmatch.py", "cy/dedupe.py", "cy/noopmod.py", "cynative.py"],
        language_level="3",
        annotate=True,
        compiler_directives={
            "annotation_typing": True,
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
            "initializedcheck": False,
            "binding": True,
            "embedsignature": True,
            "profile": False,
        },
    ),
)
