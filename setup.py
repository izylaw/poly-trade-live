from setuptools import setup, find_packages

setup(
    name="poly-trade",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "py-clob-client>=0.20.0",
        "python-dotenv>=1.0.0",
        "pydantic>=2.0.0",
        "pydantic-settings>=2.0.0",
        "click>=8.0.0",
        "rich>=13.0.0",
        "requests>=2.31.0",
    ],
    entry_points={
        "console_scripts": [
            "poly-trade=src.main:cli",
        ],
    },
)
