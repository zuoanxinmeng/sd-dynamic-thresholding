##################
# Stable Diffusion Dynamic Thresholding (CFG Scale Fix)
#
# Author: Alex 'mcmonkey' Goodwin
# GitHub URL: https://github.com/mcmonkeyprojects/sd-dynamic-thresholding
# Created: 2022/01/26
# Last updated: 2023/01/30
#
# For usage help, view the README.md file in the extension root, or via the GitHub page.
#
##################

import gradio as gr
import torch
import math
import traceback
from modules import scripts, script_callbacks, sd_samplers, sd_samplers_kdiffusion, sd_samplers_common

######################### Data values #########################
VALID_MODES = ["Constant", "Linear Down", "Cosine Down", "Half Cosine Down", "Linear Up", "Cosine Up", "Half Cosine Up", "Power Up", "Power Down"]

######################### Script class entrypoint #########################
class Script(scripts.Script):

    def title(self):
        return "Dynamic Thresholding (CFG Scale Fix)"

    def show(self, is_img2img):
        return scripts.AlwaysVisible

    def ui(self, is_img2img):
        enabled = gr.Checkbox(value=False, label="Enable Dynamic Thresholding (CFG Scale Fix)")
        # "Dynamic Thresholding (CFG Scale Fix)"
        accordion = gr.Group(visible=False)
        with accordion:
            gr.HTML(value=f"<br>View <a style=\"border-bottom: 1px #00ffff dotted;\" href=\"https://github.com/mcmonkeyprojects/sd-dynamic-thresholding/wiki/Usage-Tips\">the wiki for usage tips.</a><br><br>")
            mimic_scale = gr.Slider(minimum=1.0, maximum=30.0, step=0.5, label='Mimic CFG Scale', value=7.0)
            with gr.Accordion("Dynamic Thresholding Advanced Options", open=False):
                threshold_percentile = gr.Slider(minimum=90.0, value=100.0, maximum=100.0, step=0.05, label='Top percentile of latents to clamp')
                mimic_mode = gr.Dropdown(VALID_MODES, value="Constant", label="Mimic Scale Scheduler")
                mimic_scale_min = gr.Slider(minimum=0.0, maximum=30.0, step=0.5, label="Minimum value of the Mimic Scale Scheduler")
                cfg_mode = gr.Dropdown(VALID_MODES, value="Constant", label="CFG Scale Scheduler")
                cfg_scale_min = gr.Slider(minimum=0.0, maximum=30.0, step=0.5, label="Minimum value of the CFG Scale Scheduler")
                power_val = gr.Slider(minimum=0.0, maximum=15.0, step=0.5, value=4.0, visible=False, label="Power Scheduler Value")
        def shouldShowPowerScheduler(cfgMode, mimicMode):
            if cfgMode in ["Power Up", "Power Down"] or mimicMode in ["Power Up", "Power Down"]:
                return {"visible": True, "__type__": "update"}
            return {"visible": False, "__type__": "update"}
        cfg_mode.change(shouldShowPowerScheduler, inputs=[cfg_mode, mimic_mode], outputs=power_val)
        mimic_mode.change(shouldShowPowerScheduler, inputs=[cfg_mode, mimic_mode], outputs=power_val)
        enabled.change(
            fn=lambda x: {"visible": x, "__type__": "update"},
            inputs=[enabled],
            outputs=[accordion],
            show_progress = False)
        self.infotext_fields = (
            (enabled, lambda d: gr.Checkbox.update(value="Dynamic thresholding enabled" in d)),
            (accordion, lambda d: gr.Accordion.update(visible="Dynamic thresholding enabled" in d)),
            (mimic_scale, "Mimic scale"),
            (threshold_percentile, "Threshold percentile"),
            (mimic_scale_min, "Mimic scale minimum"),
            (mimic_mode, "Mimic mode"),
            (cfg_mode, "CFG mode"),
            (cfg_scale_min, "CFG scale minimum"),
            (power_val, "Power scheduler value"))
        return [enabled, mimic_scale, threshold_percentile, mimic_mode, mimic_scale_min, cfg_mode, cfg_scale_min, power_val]

    last_id = 0

    def process_batch(self, p, enabled, mimic_scale, threshold_percentile, mimic_mode, mimic_scale_min, cfg_mode, cfg_scale_min, powerscale_power, batch_number, prompts, seeds, subseeds):
        enabled = getattr(p, 'dynthres_enabled', enabled)
        if not enabled:
            return
        if p.sampler_name in ["DDIM", "PLMS"]:
            raise RuntimeError(f"Cannot use sampler {p.sampler_name} with Dynamic Thresholding")
        mimic_scale = getattr(p, 'dynthres_mimic_scale', mimic_scale)
        threshold_percentile = getattr(p, 'dynthres_threshold_percentile', threshold_percentile)
        mimic_mode = getattr(p, 'dynthres_mimic_mode', mimic_mode)
        mimic_scale_min = getattr(p, 'dynthres_mimic_scale_min', mimic_scale_min)
        cfg_mode = getattr(p, 'dynthres_cfg_mode', cfg_mode)
        cfg_scale_min = getattr(p, 'dynthres_cfg_scale_min', cfg_scale_min)
        experiment_mode = getattr(p, 'dynthres_experiment_mode', 0)
        power_val = getattr(p, 'dynthres_power_val', powerscale_power)
        p.extra_generation_params["Dynamic thresholding enabled"] = True
        p.extra_generation_params["Mimic scale"] = mimic_scale
        p.extra_generation_params["Threshold percentile"] = threshold_percentile
        if mimic_mode != "Constant":
            p.extra_generation_params["Mimic mode"] = mimic_mode
            p.extra_generation_params["Mimic scale minimum"] = mimic_scale_min
        if cfg_mode != "Constant":
            p.extra_generation_params["CFG mode"] = cfg_mode
            p.extra_generation_params["CFG scale minimum"] = cfg_scale_min
        if cfg_mode in ["Power Up", "Power Down"] or mimic_mode in ["Power Up", "Power Down"]:
            p.extra_generation_params["Power scheduler value"] = power_val
        # Note: the ID number is to protect the edge case of multiple simultaneous runs with different settings
        Script.last_id += 1
        fixed_sampler_name = f"{p.sampler_name}_dynthres{Script.last_id}"
        # Percentage to portion
        threshold_percentile *= 0.01
        # Make a placeholder sampler
        sampler = sd_samplers.all_samplers_map[p.sampler_name]
        def newConstructor(model):
            result = sampler.constructor(model)
            cfg = CustomCFGDenoiser(result.model_wrap_cfg.inner_model, mimic_scale, threshold_percentile, mimic_mode, mimic_scale_min, cfg_mode, cfg_scale_min, power_val, experiment_mode, p.steps)
            result.model_wrap_cfg = cfg
            return result
        newSampler = sd_samplers_common.SamplerData(fixed_sampler_name, newConstructor, sampler.aliases, sampler.options)
        # Apply for usage
        p.orig_sampler_name = p.sampler_name
        p.sampler_name = fixed_sampler_name
        p.fixed_sampler_name = fixed_sampler_name
        sd_samplers.all_samplers_map[fixed_sampler_name] = newSampler
        if p.sampler is not None:
            p.sampler = sd_samplers.create_sampler(fixed_sampler_name, p.sd_model)

    def postprocess_batch(self, p, enabled, mimic_scale, threshold_percentile, mimic_mode, mimic_scale_min, cfg_mode, cfg_scale_min, powerscale_power, batch_number, images):
        if not enabled or not hasattr(p, 'orig_sampler_name'):
            return
        p.sampler_name = p.orig_sampler_name
        del sd_samplers.all_samplers_map[p.fixed_sampler_name]
        del p.orig_sampler_name
        del p.fixed_sampler_name

######################### Implementation logic #########################

class CustomCFGDenoiser(sd_samplers_kdiffusion.CFGDenoiser):
    def __init__(self, model, mimic_scale, threshold_percentile, mimic_mode, mimic_scale_min, cfg_mode, cfg_scale_min, power_val, experiment_mode, maxSteps):
        super().__init__(model)
        self.mimic_scale = mimic_scale
        self.threshold_percentile = threshold_percentile
        self.mimic_mode = mimic_mode
        self.cfg_mode = cfg_mode
        self.maxSteps = maxSteps
        self.cfg_scale_min = cfg_scale_min
        self.mimic_scale_min = mimic_scale_min
        self.experiment_mode = experiment_mode
        self.power_val = power_val

    def combine_denoised(self, x_out, conds_list, uncond, cond_scale):
        denoised_uncond = x_out[-uncond.shape[0]:]
        return self.dynthresh(x_out[:-uncond.shape[0]], denoised_uncond, cond_scale, conds_list)
    
    def interpretScale(self, scale, mode, min):
        scale -= min
        max = self.maxSteps - 1
        if mode == "Constant":
            pass
        elif mode == "Linear Down":
            scale *= 1.0 - (self.step / max)
        elif mode == "Half Cosine Down":
            scale *= math.cos((self.step / max))
        elif mode == "Cosine Down":
            scale *= math.cos((self.step / max) * 1.5707)
        elif mode == "Linear Up":
            scale *= self.step / max
        elif mode == "Half Cosine Up":
            scale *= 1.0 - math.cos((self.step / max))
        elif mode == "Cosine Up":
            scale *= 1.0 - math.cos((self.step / max) * 1.5707)
        elif mode == "Power Up":
            scale *= math.pow(self.step / max, self.power_val)
        elif mode == "Power Down":
            scale *= 1.0 - math.pow(self.step / max, self.power_val)
        scale += min
        return scale

    def dynthresh(self, cond, uncond, cfgScale, conds_list):
        mimicScale = self.interpretScale(self.mimic_scale, self.mimic_mode, self.mimic_scale_min)
        cfgScale = self.interpretScale(cfgScale, self.cfg_mode, self.cfg_scale_min)
        # uncond shape is (batch, 4, height, width)
        conds_per_batch = cond.shape[0] / uncond.shape[0]
        assert conds_per_batch == int(conds_per_batch), "Expected # of conds per batch to be constant across batches"
        cond_stacked = cond.reshape((-1, int(conds_per_batch)) + uncond.shape[1:])
        # conds_list shape is (batch, cond, 2)
        weights = torch.tensor(conds_list, device=uncond.device).select(2, 1)
        weights = weights.reshape(*weights.shape, 1, 1, 1)

        ### Normal first part of the CFG Scale logic, basically
        diff = cond_stacked - uncond.unsqueeze(1)
        relative = (diff * weights).sum(1)

        ### Get the normal result for both mimic and normal scale
        mim_target = uncond + relative * mimicScale
        cfg_target = uncond + relative * cfgScale
        ### If we weren't doing mimic scale, we'd just return cfg_target here

        ### Now recenter the values relative to their average rather than absolute, to allow scaling from average
        mim_flattened = mim_target.flatten(2)
        cfg_flattened = cfg_target.flatten(2)
        mim_means = mim_flattened.mean(dim=2).unsqueeze(2)
        cfg_means = cfg_flattened.mean(dim=2).unsqueeze(2)
        mim_centered = mim_flattened - mim_means
        cfg_centered = cfg_flattened - cfg_means

        ### Get the maximum value of all datapoints (with an optional threshold percentile on the uncond)
        mim_max = mim_centered.abs().max(dim=2).values.unsqueeze(2)
        cfg_max = torch.quantile(cfg_centered.abs(), self.threshold_percentile, dim=2).unsqueeze(2)
        actualMax = torch.maximum(cfg_max, mim_max)

        ### Clamp to the max
        cfg_clamped = cfg_centered.clamp(-actualMax, actualMax)
        ### Now shrink from the max to normalize and grow to the mimic scale (instead of the CFG scale)
        cfg_renormalized = (cfg_clamped / actualMax) * mim_max

        ### Now add it back onto the averages to get into real scale again and return
        result = cfg_renormalized + cfg_means
        actualRes = result.unflatten(2, mim_target.shape[2:])

        if self.experiment_mode == 1:
            num = actualRes.cpu().numpy()
            for y in range(0, 64):
                for x in range (0, 64):
                    if num[0][0][y][x] > 1.0:
                        num[0][1][y][x] *= 0.5
                    if num[0][1][y][x] > 1.0:
                        num[0][1][y][x] *= 0.5
                    if num[0][2][y][x] > 1.5:
                        num[0][2][y][x] *= 0.5
            actualRes = torch.from_numpy(num).to(device=uncond.device)
        elif self.experiment_mode == 2:
            num = actualRes.cpu().numpy()
            for y in range(0, 64):
                for x in range (0, 64):
                    overScale = False
                    for z in range(0, 4):
                        if abs(num[0][z][y][x]) > 1.5:
                            overScale = True
                    if overScale:
                        for z in range(0, 4):
                            num[0][z][y][x] *= 0.7
            actualRes = torch.from_numpy(num).to(device=uncond.device)
        elif self.experiment_mode == 3:
            coefs = torch.tensor([
                #  R       G        B      W
                [0.298,   0.207,  0.208, 0.0], # L1
                [0.187,   0.286,  0.173, 0.0], # L2
                [-0.158,  0.189,  0.264, 0.0], # L3
                [-0.184, -0.271, -0.473, 1.0], # L4
            ], device=uncond.device)
            resRGB = torch.einsum("laxy,ab -> lbxy", actualRes, coefs)
            maxR, maxG, maxB, maxW = resRGB[0][0].max(), resRGB[0][1].max(), resRGB[0][2].max(), resRGB[0][3].max()
            maxRGB = max(maxR, maxG, maxB)
            print(f"test max = r={maxR}, g={maxG}, b={maxB}, w={maxW}, rgb={maxRGB}")
            if self.step / (self.maxSteps - 1) > 0.2:
                if maxRGB < 2.0 and maxW < 3.0:
                    resRGB /= maxRGB / 2.4
            else:
                if maxRGB > 2.4 and maxW > 3.0:
                    resRGB /= maxRGB / 2.4
            actualRes = torch.einsum("laxy,ab -> lbxy", resRGB, coefs.inverse())

        return actualRes

######################### XYZ Plot Script Support logic #########################

def make_axis_options():
    xyz_grid = [x for x in scripts.scripts_data if x.script_class.__module__ == "xyz_grid.py"][0].module
    def apply_mimic_scale(p, x, xs):
        if x != 0:
            setattr(p, "dynthres_enabled", True)
            setattr(p, "dynthres_mimic_scale", x)
        else:
            setattr(p, "dynthres_enabled", False)
    def confirm_scheduler(p, xs):
        for x in xs:
            if x not in VALID_MODES:
                raise RuntimeError(f"Unknown Scheduler: {x}")
    extra_axis_options = [
        xyz_grid.AxisOption("[DynThres] Mimic Scale", float, apply_mimic_scale),
        xyz_grid.AxisOption("[DynThres] Threshold Percentile", float, xyz_grid.apply_field("dynthres_threshold_percentile")),
        xyz_grid.AxisOption("[DynThres] Mimic Scheduler", str, xyz_grid.apply_field("dynthres_mimic_mode"), confirm=confirm_scheduler, choices=lambda: VALID_MODES),
        xyz_grid.AxisOption("[DynThres] Mimic minimum", float, xyz_grid.apply_field("dynthres_mimic_scale_min")),
        xyz_grid.AxisOption("[DynThres] CFG Scheduler", str, xyz_grid.apply_field("dynthres_cfg_mode"), confirm=confirm_scheduler, choices=lambda: VALID_MODES),
        xyz_grid.AxisOption("[DynThres] CFG minimum", float, xyz_grid.apply_field("dynthres_cfg_scale_min")),
        xyz_grid.AxisOption("[DynThres] Power scheduler value", float, xyz_grid.apply_field("dynthres_power_val"))
    ]
    if not any("[DynThres]" in x.label for x in xyz_grid.axis_options):
        xyz_grid.axis_options.extend(extra_axis_options)

def callbackBeforeUi():
    try:
        make_axis_options()
    except Exception as e:
        traceback.print_exc()
        print(f"Failed to add support for X/Y/Z Plot Script because: {e}")

script_callbacks.on_before_ui(callbackBeforeUi)
