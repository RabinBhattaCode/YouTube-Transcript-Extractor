# Drum Replacement Coursework

This folder contains the report, notebooks and Python files for my drum replacement system.

## Start here

Open the report:

```text
Drum_Replacement_using_U_Net_Separation_and_AST_Classification.pdf
```

Then open the notebooks in this order:

```text
1.Train_Separator.ipynb
2.Demo_Remix.ipynb
3.Eval_Metrics.ipynb
```

## Install packages

If anything is missing, run:

```bash
pip install -r requirements.txt
```

## Main files

The notebooks import the Python code from `MAIN/src`.

```text
MAIN/src/separator.py        U-Net drum separator
MAIN/src/ast_classifier.py   AST loop choice
MAIN/src/remix_demo.py       final remix demo
MAIN/src/ast_eval.py         AST evaluation helper
MAIN/src/config.py           paths and settings
```

The trained checkpoint is:

```text
MAIN/checkpoints/unet_separator_stft_midfocus.pt
```

## Data

The full MUSDB data is not included because it is too large. The 7-second MUSDB sample can be downloaded using the official `musdb` package.

Official musdb GitHub:
https://github.com/sigsep/sigsep-mus-db

The downloaded 7-second sample should be placed here:

Data/MUSDB18_7s_sample/

Input audio and drum loops should be placed here:

Data/InputAudio/
Data/DrumLoops/
