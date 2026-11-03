# Credits and licences

This product is built and maintained by Brandzaha. It uses these open-source
components, each under its own licence:

| Component | Used for | Licence |
|---|---|---|
| [Leffa](https://github.com/franciszzj/Leffa) | Free photo try-on on the current site | MIT |
| [FASHN VTON v1.5](https://github.com/fashn-AI/fashn-vton-1.5) | Photo try-on in the next version (`virtual-tryon/`) | Apache-2.0 |
| [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) | 4K upscaling in the next version | BSD-3-Clause |
| [CLIP](https://github.com/openai/CLIP) | Fit assistant in the next version | MIT |
| [MediaPipe](https://github.com/google-ai-edge/mediapipe) (selfie multiclass segmentation, pose landmarker) | Body and clothing parsing: keeps face, hair and hands, finds the garment area | Apache-2.0 |
| [DWPose](https://github.com/IDEA-Research/DWPose) / [YOLOX](https://github.com/Megvii-BaseDetection/YOLOX) (inside FASHN VTON) | Body pose for the try-on model | Apache-2.0 |

The FASHN Human Parser (SegFormer weights, NVIDIA non-commercial licence) is **not**
used: `virtual-tryon/shims/fashn_human_parser` stands in for it with the MediaPipe
parser above, for both our code and FASHN's own preprocessing.
| Sample garment photos in `public/garments/` | Demo catalog | MIT, see `public/garments/LICENSE-tryon-examples.txt` |

README images and animations in `docs/assets/` are original artwork made for this project.
