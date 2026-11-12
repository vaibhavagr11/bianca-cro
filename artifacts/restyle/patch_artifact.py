#!/usr/bin/env python3
"""Restyle Eren's Sales Pipeline Updates artifact pages (branch GSheet-Updates) to the War Room 1 layout,
with a Focus (card grid) theme, without touching data access, the connector hook, or approve / ignore semantics.

Usage: python3 patch_artifact.py <dir with Eren's originals> <out dir>
Inputs : sales-pipeline-updates.html, sales-pipeline-updates-demo.html
Outputs: same two file names, restyled. Presentation only: <style>, header / toolbar / dialog markup,
         card() and the review-list shell, demo seed (real Nov 3 companies), owner label for NDL, toast wording.
"""
import re, sys, os, base64, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seed_data import to_js

SHEET_URL = "https://docs.google.com/spreadsheets/d/11HXJsfGHIqnr_mekiV6l277v61C2qz_ELNxA0RyaNcA/edit?gid=1869050409#gid=1869050409"
HERE = os.path.dirname(os.path.abspath(__file__))
LOGO_BLUE = base64.b64encode(open(os.path.join(HERE, "bianca-blue-360.png"), "rb").read()).decode()
LOGO_WHITE = base64.b64encode(open(os.path.join(HERE, "bianca-white-360.png"), "rb").read()).decode()

# ------------------------------------------------------------------------------------------------ CSS
CSS = r"""
:root{--blue:#2D62F1;--blue-dark:#1F4BD6;--navy:#161C42;--ink:#1B2238;--muted:#66718A;--line:#E1E6F0;--soft:#F3F6FD;--bg:#F6F8FC;--green:#167356;--green-dark:#115C44;--red:#B33346;--amber:#8A5A00;--rg-bg:#E9EEFF;--rg:#3459BC;--nc-bg:#EAF4F0;--nc:#397C68;--ndl-bg:#F3ECFF;--ndl:#6B4BB8}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}body{margin:0;font-family:Inter,-apple-system,"Segoe UI",Arial,sans-serif;color:var(--ink);background:var(--bg);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
h1,h2,h3,p,ul{margin:0}button,input,textarea,select{font:inherit}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.5}a{color:var(--blue);text-decoration:none}[hidden]{display:none!important}
button:focus-visible,a:focus-visible,summary:focus-visible,input:focus-visible,textarea:focus-visible{outline:3px solid #829CFF;outline-offset:2px}
.sr-only{position:absolute;clip:rect(0,0,0,0);width:1px;height:1px;padding:0;overflow:hidden;white-space:nowrap}
.pp{font-family:Poppins,Inter,Arial,sans-serif}
/* header */
.site-header{background:var(--navy);color:#fff}.header-inner{max-width:1240px;margin:auto;padding:12px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:60px}
.brand{display:flex;align-items:center;gap:12px;color:#fff;font-weight:600;text-decoration:none}.brand img{height:26px;width:auto;display:block}.brand .logo-blue{display:none}.brand-sub{font-size:12px;color:#9FB4FF;border-left:1px solid #ffffff33;padding-left:12px;line-height:1.2}
.header-right{display:flex;align-items:center;gap:14px;font-size:12px;color:#C9D6FF}.people{display:flex;padding-left:4px}
.avatar{display:inline-grid;place-items:center;width:26px;height:26px;border-radius:50%;font-size:9px;font-weight:700;border:2px solid #fff;margin-left:-6px;background:var(--rg-bg);color:var(--rg)}.avatar.nc{background:var(--nc-bg);color:var(--nc)}.avatar.ndl{background:var(--ndl-bg);color:var(--ndl)}
.strip{background:#0E1630;color:#C9D6FF;font-size:12px}.strip-in{max-width:1240px;margin:auto;padding:8px 24px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}.strip b{color:#fff;font-weight:600}.strip .sheet-link{margin-left:auto;display:inline-flex;align-items:center;gap:6px;background:var(--blue);color:#fff;border-radius:6px;padding:5px 11px;font-weight:600;white-space:nowrap}.strip .ill{color:#8FA3E0;font-size:10px}
/* page */
.page{max-width:1240px;margin:auto;padding:22px 24px 60px}
.eyebrow{display:none}.page-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}.page-heading h1{font-family:Poppins,Inter,sans-serif;font-size:24px;font-weight:600;letter-spacing:-.4px;line-height:1.2}.intro{font-size:13px;color:var(--muted);margin-top:4px;max-width:640px}
.demo-badge{border:1px solid #DCE4FF;color:#4C63A0;background:#EEF3FF;font-size:11px;font-weight:600;padding:5px 9px;border-radius:6px;white-space:nowrap}
.demo-caption{margin-top:12px;padding:8px 12px;border-left:2px solid #9FB4FF;background:#EEF3FF;color:#586889;font-size:12px;line-height:1.6;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.connection-actions{display:flex;gap:12px;align-items:center}
.text-control{border:0;background:transparent;padding:2px 0;color:var(--blue);font-size:12px;text-decoration:underline;text-underline-offset:3px;white-space:nowrap}
.connection-panel{margin-top:14px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px 18px}.connection-panel h2{font-size:15px;font-weight:650}.connection-panel p{font-size:12.5px;color:var(--muted);margin-top:4px}.sign-in-controls{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}.sign-in-controls input{flex:1;min-width:220px;height:38px;border:1px solid var(--line);border-radius:7px;padding:0 10px}
.sync-notice{margin-top:12px;background:#FFF8E8;border:1px solid #F0D9A4;border-radius:8px;padding:10px 12px;font-size:12.5px;color:#5C4300}
.tabbar{display:flex;align-items:center;gap:24px;border-bottom:1px solid var(--line);margin-top:18px}.tab{position:relative;padding:0 0 11px;border:0;background:transparent;font-size:13px;font-weight:600;color:var(--muted);display:flex;align-items:center;gap:7px;white-space:nowrap}.tab[aria-selected=true]{color:var(--blue)}.tab[aria-selected=true]:after{content:"";height:3px;background:var(--blue);position:absolute;bottom:-1px;left:0;right:0;border-radius:3px 3px 0 0}.tab-count{font-size:11px;padding:1px 6px;border-radius:5px;background:#E8EDFB;min-width:20px;text-align:center}
/* toolbar: segmented owner control + hidden legacy controls */
.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:14px 0 10px;flex-wrap:wrap}.search-wrap,.owner-filter{display:none!important}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;background:#fff;overflow:hidden}.seg button{border:0;background:#fff;padding:8px 13px;font-size:12.5px;font-weight:600;color:#55617C;display:flex;gap:7px;align-items:center;border-right:1px solid var(--line);white-space:nowrap}.seg button:last-child{border-right:0}.seg button .avatar{margin:0;width:20px;height:20px;font-size:8px;border-width:1px}.seg button.on{background:var(--soft);color:var(--blue);box-shadow:inset 0 -2px 0 var(--blue)}.seg button.on.rg{color:var(--rg)}.seg button.on.nc{color:var(--nc)}.seg button.on.ndl{color:var(--ndl)}.seg .cnt{font-size:11px;background:#EEF1F7;border-radius:5px;padding:0 6px;color:#55617C}
.tb-right{display:flex;align-items:center;gap:8px}.layout-toggle{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}.layout-toggle button{border:0;background:#fff;padding:7px 10px;font-size:11.5px;font-weight:600;color:#55617C}.layout-toggle button.on{background:var(--navy);color:#fff}
.results-line{display:none}.kbd{display:inline-block;border:1px solid #CBD3E3;border-bottom-width:2px;border-radius:4px;padding:0 5px;font-size:10.5px;line-height:16px;color:#55617C;background:#fff;font-weight:600}
.proposed{display:inline-block;font-size:9.5px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;line-height:15px;padding:0 5px;border-radius:3px;background:#FFF1D6;color:#8A5A00;border:1px dashed #E0B460;vertical-align:middle;margin-left:6px}
/* war-room shell */
.wr{display:grid;grid-template-columns:320px 1fr;gap:18px;align-items:start}
.wr-side{background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden;position:sticky;top:12px;max-height:calc(100vh - 24px);overflow-y:auto}
.wr-side .lh{padding:12px 14px 8px;font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#8792A8;display:flex;justify-content:space-between}
.wr-grp{padding:8px 14px 4px;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:#8792A8;display:flex;align-items:center;gap:8px;border-top:1px solid var(--line)}.wr-grp:first-of-type{border-top:0}.wr-grp .avatar{margin:0;width:20px;height:20px;font-size:8px;border-width:1px}
.wr-row{display:block;width:100%;text-align:left;background:#fff;border:0;border-left:3px solid transparent;padding:10px 14px 10px 12px;cursor:pointer}.wr-row:hover{background:#F6F8FD}.wr-row.on{background:var(--soft);border-left-color:var(--blue)}.wr-row .r1{display:flex;justify-content:space-between;gap:8px;align-items:center}.wr-row .co{font-family:Poppins,Inter,sans-serif;font-weight:600;font-size:13.5px;color:var(--ink)}.wr-row .age{font-size:11px;color:#94A0B2;white-space:nowrap}.wr-row .r2{font-size:12px;color:#5E6B86;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.wr-row .r3{margin-top:6px;display:flex;gap:6px;align-items:center;font-size:11px;color:#55617C;font-weight:600}.wr-row .r3 .own{margin-left:-2px}.wr-row .r3 .avatar{margin:0;width:18px;height:18px;font-size:7.5px;border-width:1px}
.wr-main{min-width:0}.wr-nav{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}.wr-nav .pos{font-size:12px;color:var(--muted)}.wr-nav .btns{display:flex;gap:6px}.pill{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 12px;border-radius:999px;border:1px solid var(--line);background:#fff;font-size:12px;font-weight:600;color:var(--ink)}.pill.primary{background:var(--navy);color:#fff;border-color:var(--navy)}.pill:disabled{opacity:.4}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:14px}
/* card */
.review-card{background:#fff;border:1px solid #D7DFEE;border-radius:12px;padding:18px 20px 14px;box-shadow:0 1px 2px #1B22380A;position:relative}.wr .review-card{border-top:3px solid var(--blue)}
.card-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.company-title{font-family:Poppins,Inter,sans-serif;font-size:19px;font-weight:600;letter-spacing:-.2px;line-height:1.3}
.meta-bar{display:flex;align-items:center;gap:0;margin-top:8px;font-size:12px;color:var(--muted);flex-wrap:wrap}.meta-bar>span{display:inline-flex;align-items:center;gap:6px;padding:0 12px;border-left:1px solid var(--line)}.meta-bar>span:first-child{padding-left:0;border-left:0}.meta-bar .k{font-weight:600;color:#8792A8}.meta-bar .avatar{margin:0;width:20px;height:20px;font-size:8px;border-width:1px}
.stage{display:inline-flex;align-items:center;white-space:nowrap;border-radius:5px;padding:2px 8px;font-size:11px;font-weight:600;line-height:1.5;background:#EEF1F7;color:#3D424B}.stage-0{background:#FFCFC9;color:#8B2923}.stage-1{background:#BFE1F6;color:#214C67}.stage-2{background:#FFE5A0;color:#654B11}.stage-3{background:#E6CFF2;color:#59316D}.stage-4{background:#D4EDBC;color:#355A32}.stage-5{background:#11734B;color:#fff}
.sec-label{margin-top:16px;font-size:10.5px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#8792A8;display:flex;align-items:center;gap:8px;flex-wrap:wrap}.sec-label .hint{font-weight:500;letter-spacing:0;text-transform:none;color:#A0A9BC}
.bullet-list{list-style:none;padding:0;margin:8px 0 0}.bullet-list li{position:relative;padding-left:16px;margin:4px 0}.bullet-list li:before{content:"-";position:absolute;left:2px;color:currentColor;font-weight:500}
.ns-box{margin-top:14px;background:#FFF9E3;border:1px solid #EFDFA6;border-radius:8px;padding:10px 12px 8px}.ns-box .sec-label{margin-top:0;color:#7A5A12}.ns-box .bullet-list{margin-top:6px}.bullet-list[data-destination=nextStep]{background:transparent;border:0;padding:0}.bullet-list[data-destination=nextStep] li{font-size:14.5px;line-height:1.55;color:var(--ink);font-weight:450}
.bullet-list[data-destination=clientContext] li{font-size:13px;line-height:1.55;color:#5E6B86;font-weight:400}
.source-highlight{background:#FFF4D6;border-radius:4px}
.empty-group{font-size:12.5px;color:#94A0B2;margin-top:6px;font-style:italic}
.whats-new{list-style:none;padding:0;margin:8px 0 0}.whats-new li{margin:3px 0}.whats-new details{font-size:13px;color:#5E6B86;line-height:1.5}.whats-new summary{list-style:none;cursor:pointer;display:flex;gap:8px;align-items:flex-start;padding-left:16px;position:relative}.whats-new summary::-webkit-details-marker{display:none}.whats-new summary:before{content:"-";position:absolute;left:2px;top:0;color:currentColor;font-weight:500}.whats-new .src{display:inline-block;font-size:10.5px;color:#5874AD;background:#F3F6FF;border:1px solid #E5EBF7;border-radius:4px;padding:0 6px;line-height:17px;white-space:nowrap;flex:none;margin-top:2px}.whats-new .ctx{margin:6px 0 4px 16px;padding:8px 10px;background:#F7F9FD;border-left:2px solid #C9D4EA;font-size:12.5px;color:#44506B;border-radius:0 6px 6px 0}
.source-list{display:flex;flex-wrap:wrap;gap:6px 8px;list-style:none;padding:0;margin:12px 0 0}.source-label{font-size:11px;color:#5874AD;background:#F3F6FF;border:1px solid #E5EBF7;border-radius:4px;padding:2px 8px;line-height:1.6;cursor:pointer}.source-label[aria-pressed=true]{background:var(--blue);color:#fff;border-color:var(--blue)}.source-label:disabled{opacity:.55;cursor:default}
.source-trace-status{font-size:11px;color:#94A0B2;margin-top:6px}.source-trace-unavailable{color:var(--amber)}
.approval-note{margin-top:10px;font-size:12px;color:var(--amber);background:#FFF8E8;border:1px solid #F0D9A4;border-radius:6px;padding:8px 10px}.legacy-note{color:var(--red);background:#FDF0F2;border-color:#F3C9D0}
details.current-next-steps{margin-top:10px;border-top:1px solid #EFF2F8;padding-top:9px;font-size:12.5px}details.current-next-steps summary{cursor:pointer;color:#62769A;font-weight:600;list-style:none;display:flex;gap:8px;align-items:center}details.current-next-steps summary::-webkit-details-marker{display:none}details.current-next-steps summary:before{content:"+";width:12px;color:#8C9CB7;font-size:15px}details.current-next-steps[open] summary:before{content:"-"}
.current-next-steps-content{font-size:12.5px;line-height:1.7;color:#5E6F8B;background:#F7F9FD;border-radius:6px;padding:10px 12px;margin-top:8px;white-space:pre-wrap;overflow-wrap:anywhere}
.cells-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:8px}.cells-grid h4{margin:0 0 4px;font-size:10.5px;color:#8792A8;font-weight:700;text-transform:uppercase;letter-spacing:.6px}.cells-grid .current-next-steps-content{margin-top:0;min-height:46px}
.card-footer{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:14px;flex-wrap:wrap}.actions{display:flex;gap:8px;align-items:center;position:relative;flex-wrap:wrap}
.primary,.secondary,.quiet{min-height:36px;border-radius:7px;padding:0 14px;font-size:13px;font-weight:600;line-height:1.2;display:inline-flex;align-items:center;justify-content:center;gap:6px;border:1px solid transparent}
.primary{background:var(--green);border-color:var(--green);color:#fff}.primary:hover:not(:disabled){background:var(--green-dark)}.secondary{color:var(--ink);background:#fff;border-color:#CBD3E3}.secondary:hover:not(:disabled){background:var(--soft)}.quiet{color:#6B7690;background:#fff;border-color:transparent;padding:0 10px}.quiet:hover:not(:disabled){background:var(--soft)}
.approve-check{font-size:13px;font-weight:400}
.ign-menu{display:none;position:absolute;left:0;top:42px;width:310px;background:#fff;border:1px solid var(--line);border-radius:10px;box-shadow:0 14px 40px #14234A26;padding:6px;z-index:6}.ign-menu.open{display:block}.ign-menu .mh{font-size:10.5px;color:#8792A8;font-weight:700;text-transform:uppercase;letter-spacing:.6px;padding:6px 8px;display:flex;align-items:center}.ign-menu .mi{display:block;width:100%;text-align:left;background:none;border:0;padding:8px 10px;border-radius:7px}.ign-menu .mi:hover{background:var(--soft)}.ign-menu .mi b{display:block;font-size:13px;font-weight:600;color:var(--ink)}.ign-menu .mi span{display:block;font-size:11.5px;color:var(--muted)}
.rowlink{font-size:12px;color:var(--muted)}.rowlink a{font-weight:600}
.card-error{color:var(--red);font-size:12px;margin-top:8px}.validation{color:var(--red);font-size:12px;line-height:1.5;margin-top:8px}
.empty-state{text-align:center;border:1px dashed #CCD8EC;background:#fff;border-radius:12px;padding:44px 24px;margin:10px 0}.empty-icon{width:40px;height:40px;background:#EEF3FF;color:var(--blue);border-radius:50%;margin:0 auto 12px;display:grid;place-items:center;font-size:18px}.empty-state h2{font-size:17px}.empty-state p{font-size:13px;line-height:1.7;color:var(--muted);margin:8px auto 16px;max-width:420px}
/* activity */
.panel-intro{margin:18px 0 10px;display:flex;justify-content:space-between;align-items:center;gap:15px}.panel-intro h2{font-size:16px;font-weight:650}.panel-intro p{font-size:12.5px;color:var(--muted);margin-top:4px}
.activity-list{background:#fff;border:1px solid var(--line);border-radius:12px;padding:0 18px}.activity-row{display:flex;gap:14px;padding:14px 0;border-bottom:1px solid var(--line);align-items:flex-start}.activity-row:last-child{border-bottom:0}.activity-symbol{display:grid;place-items:center;flex:none;width:30px;height:30px;border-radius:50%;font-size:12px;background:#EBF5F0;color:var(--green)}.activity-symbol.ignored{background:#EDF0F6;color:#8190AA}
.activity-company{font-family:Poppins,Inter,sans-serif;font-size:14px;font-weight:600}.activity-description{font-size:12.5px;color:var(--muted);margin-top:3px}.activity-time{margin-left:auto;font-size:11px;white-space:nowrap;color:#94A0B2;padding-top:4px}.activity-details{font-size:11.5px;color:#7A88A0;margin-top:6px}.activity-details .rotation-content{white-space:pre-line;background:#F7F9FD;border-radius:6px;padding:8px 10px;margin-top:6px;color:#44506B}
.storage-note{margin-top:14px;font-size:11px;color:#8B6A3C}
.page-footer{margin-top:28px;padding-top:14px;border-top:1px solid var(--line);font-size:11px;color:#99A4B8;display:flex;justify-content:space-between;gap:18px;flex-wrap:wrap}.footer-brand{font-weight:650;color:#7D8BA5}
/* dialog */
dialog{border:1px solid #D8E0EF;border-radius:14px;box-shadow:0 22px 90px #182D5B33;padding:0;width:700px;max-width:calc(100vw - 28px);max-height:calc(100dvh - 40px);color:var(--ink);background:#fff}dialog::backdrop{background:#14234A66;backdrop-filter:blur(2px)}
.dialog-header{padding:20px 24px 14px;border-bottom:1px solid #EDF0F7;display:flex;justify-content:space-between;gap:12px}.dialog-header h2{font-family:Poppins,Inter,sans-serif;font-size:19px;font-weight:600}.dialog-kicker{font-size:10px;color:var(--blue);font-weight:700;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px}.dialog-header p{font-size:12.5px;color:var(--muted);margin-top:6px}
.close-dialog{width:28px;height:28px;flex:none;display:grid;place-items:center;border:0;border-radius:6px;background:#F3F5FA;color:#8390AA;font-size:19px}
.dialog-body{padding:16px 24px 20px}.dialog-body label{font-size:12.5px;font-weight:650;display:block;margin:12px 0 6px}.dialog-body label:first-child{margin-top:0}
.editor{display:block;width:100%;min-height:110px;padding:10px 12px;border:1px solid #BDCCEF;border-radius:8px;font-size:13.5px;line-height:1.7;color:#344766;background:#FCFDFF;resize:vertical}.field-help{font-size:11.5px;line-height:1.6;color:#8B97AD;margin-top:6px}
.mode{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-top:12px}.mode button{border:0;background:#fff;padding:7px 12px;font-size:12px;font-weight:600;color:#55617C;border-right:1px solid var(--line)}.mode button:last-child{border-right:0}.mode button.on{background:var(--soft);color:var(--blue)}
.mode-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:8px}.mode-row .mode{margin-top:0}.mode-hint{font-size:11.5px;color:#6B7690}
.ns-rows{display:flex;flex-direction:column;gap:8px;margin-top:6px}
.ns-row{display:grid;grid-template-columns:minmax(0,1fr) 158px 32px;gap:8px;align-items:start}
.ns-row textarea{min-height:52px;border:1px solid #BDCCEF;border-radius:8px;padding:7px 10px;font:inherit;font-size:13.5px;line-height:1.45;color:#344766;background:#FCFDFF;resize:vertical;min-width:0}
.ns-row textarea:focus{outline:2px solid var(--blue);outline-offset:1px}
.ns-row input{height:36px;border:1px solid #BDCCEF;border-radius:8px;padding:0 10px;font:inherit;font-size:13.5px;color:#344766;background:#FCFDFF;min-width:0}
.ns-row input:focus{outline:2px solid var(--blue);outline-offset:1px}
.ns-row .ns-del{margin-top:2px;height:32px;width:32px;border:1px solid var(--line);border-radius:8px;background:#fff;color:#6B7690;font-size:16px;line-height:1}
.ns-head{display:grid;grid-template-columns:minmax(0,1fr) 158px 32px;gap:8px;margin-top:8px;font-size:11px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;color:#6B7690}
.ns-add{margin-top:8px}
.ns-preview{margin-top:10px;padding:8px 10px;border:1px dashed var(--line);border-radius:8px;font-size:12px;line-height:1.6;color:#55617C;white-space:pre-wrap}
.ns-preview b{display:block;font-size:11px;letter-spacing:.4px;text-transform:uppercase;color:#6B7690;margin-bottom:2px}
@media (max-width:560px){.ns-row,.ns-head{grid-template-columns:minmax(0,1fr) 132px 32px}}
.due{display:flex;gap:10px;align-items:center;margin-top:12px;font-size:12.5px;flex-wrap:wrap}.due input{height:32px;border:1px solid var(--line);border-radius:7px;padding:0 10px;font-size:12.5px;width:150px}
.rotation-preview{margin-top:16px;border:1px solid #E2E8F4;border-radius:8px;overflow:hidden}.rotation-preview h3{font-size:11px;font-weight:650;padding:9px 12px;background:#F4F7FC;color:#526786;border-bottom:1px solid #E2E8F4}.rotation-row{padding:9px 12px;display:grid;grid-template-columns:110px 1fr;gap:10px;font-size:12px;line-height:1.6}.rotation-row+.rotation-row{border-top:1px solid #EDF1F8}.rotation-name{font-weight:650;color:#697B9A}.rotation-content{color:#7A879E;white-space:pre-line;overflow-wrap:anywhere}.rotation-explain{font-size:10.5px;color:#98A3B6;margin-top:8px;line-height:1.7}
.dialog-actions{padding:12px 24px;border-top:1px solid #E9EEF7;display:flex;justify-content:flex-end;gap:8px;background:#FCFDFF}
.toast-region{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);z-index:20;max-width:calc(100vw - 30px);pointer-events:none}.toast{background:#17274B;border:1px solid #33456B;border-radius:8px;box-shadow:0 7px 30px #14234A26;color:#fff;padding:12px 16px;display:flex;align-items:center;gap:12px;font-size:12.5px;line-height:1.6;pointer-events:auto}.toast button{background:transparent;border:0;color:#B9CCFF;text-decoration:underline;text-underline-offset:3px;padding:4px;font-size:12px;white-space:nowrap}.toast-close{font-size:17px!important;text-decoration:none!important;color:#A5B2CF!important}
.pending-write{opacity:.65;pointer-events:none}
/* focus theme */
body[data-theme=focus] .site-header{background:#fff;color:var(--ink);border-bottom:1px solid var(--line)}body[data-theme=focus] .brand{color:var(--ink)}body[data-theme=focus] .brand .logo-white{display:none}body[data-theme=focus] .brand .logo-blue{display:block}body[data-theme=focus] .brand-sub{color:var(--muted);border-left-color:var(--line)}body[data-theme=focus] .header-right{color:var(--muted)}body[data-theme=focus] .avatar{border-color:#fff}
body[data-theme=focus] .wr{display:block}body[data-theme=focus] .wr-side,body[data-theme=focus] .wr-nav{display:none}body[data-theme=focus] .review-card{border-top:1px solid #D7DFEE;border-left:4px solid var(--blue)}
body[data-theme=focus] .company-title{font-size:18px}
@media(max-width:980px){.wr{grid-template-columns:1fr}.wr-side{position:static;max-height:40vh}.grid{grid-template-columns:1fr}}
@media(max-width:640px){.header-inner,.strip-in,.page{padding-left:14px;padding-right:14px}.people{display:none}.strip .sheet-link{margin-left:0}.page-heading h1{font-size:21px}.review-card{padding:14px 14px 12px}.cells-grid{grid-template-columns:1fr}.actions{flex-wrap:wrap}.primary,.secondary,.quiet{min-height:42px}.seg{overflow-x:auto;max-width:100%}.seg button{padding:8px 10px}.meta-bar>span{padding:0 8px}.dialog-body{padding:14px}.rotation-row{grid-template-columns:90px 1fr}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
@media print{.toolbar,.actions,.demo-caption,.tabbar,.page-footer,.toast-region,.wr-side,.wr-nav{display:none}.review-card{break-inside:avoid;box-shadow:none}}
"""

FONTS = '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@500;600&family=Inter:wght@400;450;500;600;700&display=swap">'

# ------------------------------------------------------------------------------------------------ markup
HEADER = '''<header class="site-header"><div class="header-inner"><a class="brand" href="#" aria-label="Bianca sales pipeline updates home"><img class="logo-white" alt="Bianca" src="data:image/png;base64,__LOGO_WHITE__"><img class="logo-blue" alt="" aria-hidden="true" src="data:image/png;base64,__LOGO_BLUE__"><span class="brand-sub">Commercial<br>Intelligence</span></a><div class="header-right"><span class="workspace-label">Founders' workspace</span><div class="people" aria-label="Ramiro Godfrid, Nico Casaux and Nicolás de León"><span class="avatar">RG</span><span class="avatar nc">NC</span><span class="avatar ndl">NDL</span></div></div></div></header>
<div class="strip"><div class="strip-in" id="wr-strip"><span><b id="wr-strip-pending">0</b> to review</span><span><b id="wr-strip-decided">0</b> decided today</span><span>Sheet synced <b>07:15 ART</b><span class="ill"> *</span></span><span>Brief sent <b>08:30 ART</b><span class="ill"> *</span></span><a class="sheet-link" href="__SHEET_URL__" target="_blank" rel="noopener">Open the Sheet copy &#8599;</a></div></div>'''

TOOLBAR = '''<div class="toolbar"><div class="seg" id="owner-seg" role="group" aria-label="Filter by owner"><button type="button" class="on" data-owner="all">All <span class="cnt" data-cnt="all">0</span></button><button type="button" class="rg" data-owner="RG"><span class="avatar">RG</span>Ramiro <span class="cnt" data-cnt="RG">0</span></button><button type="button" class="nc" data-owner="NC"><span class="avatar nc">NC</span>Nico <span class="cnt" data-cnt="NC">0</span></button><button type="button" class="ndl" data-owner="NDL"><span class="avatar ndl">NDL</span>Nicolás de León <span class="cnt" data-cnt="NDL">0</span></button></div><div class="tb-right">__LAYOUT_TOGGLE__</div><div class="search-wrap"><label class="sr-only" for="company-search">Search companies</label><input class="search" id="company-search" type="search" value="" autocomplete="off" tabindex="-1" aria-hidden="true"></div><label class="sr-only" for="owner-filter">Filter by owner</label><select class="owner-filter" id="owner-filter" tabindex="-1" aria-hidden="true"><option value="all">All founders</option><option value="RG">Ramiro · RG</option><option value="NC">Nico · NC</option><option value="NDL">Nicolás de León · NDL</option></select></div>'''
LAYOUT_TOGGLE = '<div class="layout-toggle" role="group" aria-label="Layout"><button type="button" class="on" data-theme-pick="war-room">War room</button><button type="button" data-theme-pick="focus">Focus</button></div>'

DIALOG_EXTRA = '''<div class="mode-row"><div class="mode" id="edit-mode" role="group" aria-label="Next Step write mode"><button type="button" class="on" data-mode-pick="replace" aria-pressed="true">Replace Next Step</button><button type="button" data-mode-pick="append" aria-pressed="false">Keep current bullets + add these</button></div></div><p class="field-help">Replace writes only these steps to Next Step. Keep + add leaves the bullets already in the Sheet and adds these below them. Either way the previous Next Step is copied to Last progress. The same switch is on the card.</p>'''
ROWS_EDITOR = '''<div class="ns-head" aria-hidden="true"><span>Next step</span><span>Complete by</span><span></span></div><div class="ns-rows" id="ns-rows"></div><button type="button" class="pill ns-add" data-action="ns-add">+ Add next step</button><div class="ns-preview" id="ns-preview" aria-live="polite"></div>'''
ROWS_HELP = 'One row per next step, with an optional date. The date is written into the Next Step cell after the step as "(complete by Nov 4, 2026)" - no new Sheet column. Leave every row empty to keep Next Step and Last progress unchanged.'

# ------------------------------------------------------------------------------------------------ presentation module (shared)
WR_JS = r"""
/* Presentation layer added 11/04/2026 (Kellogg team): War Room / Focus shell, segmented owner filter, sidebar, keyboard, ignore menu.
   It never calls the connector or changes decisions; every data action still flows through the existing data-action buttons. */
const BiancaWarRoom = (() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const OWNER = {RG:{name:'Ramiro',cls:''},NC:{name:'Nico',cls:'nc'},NDL:{name:'Nicolás de León',cls:'ndl'}};
  const stageClass = stage => { const m = /^(\d)\./.exec(String(stage||'')); return m ? 'stage stage-'+m[1] : 'stage'; };
  const avatar = code => { const o = OWNER[code] || {name:code||'Unassigned',cls:''}; return `<span class="avatar ${o.cls}" aria-hidden="true">${esc(code||'?')}</span>`; };
  const ownerName = code => (OWNER[code] || {name: code || 'Unassigned'}).name;
  let selected = null;
  const theme = () => { const b = document.body; if(!b || !b.dataset) return 'focus'; return b.dataset.theme === 'focus' ? 'focus' : 'war-room'; };
  function latestDate(p){ const d = (p.sources||[]).map(s => Date.parse(s.date)).filter(Number.isFinite); return d.length ? new Date(Math.max(...d)) : null; }
  function age(p){ const d = latestDate(p); if(!d) return ''; const days = Math.max(0, Math.round((Date.now() - d.getTime())/86400000)); return days === 0 ? 'today' : days === 1 ? '1d' : days+'d'; }
  function shortDate(v){ const t = Date.parse(v); return Number.isFinite(t) ? new Date(t).toLocaleDateString([], {month:'short', day:'numeric', timeZone:'UTC'}) : String(v||''); }
  // sidebar + detail (war-room) or grid (focus). `filtered` = pending proposals after the owner filter; `card` = the page's card renderer.
  function shell(filtered, card, state){
    syncControls(state, filtered);
    if(!filtered.length){ selected = null; return ''; }
    if(!filtered.some(p => p.id === selected)) selected = null;
    if(theme() === 'focus') return `<div class="grid">${filtered.map(card).join('')}</div>`;
    const ageDays = p => { const d = latestDate(p); return d ? Math.max(0, Math.round((Date.now() - d.getTime())/86400000)) : 9999; };
    const ordered = [...filtered].sort((a, b) => ageDays(a) - ageDays(b) || a.company.localeCompare(b.company));
    if(selected === null) selected = ordered[0].id;
    const idx = ordered.findIndex(p => p.id === selected), cur = ordered[idx];
    const rows = ordered.map(p => `<button type="button" class="wr-row${p.id===selected?' on':''}" data-action="wr-select" data-select="${esc(p.id)}" aria-current="${p.id===selected?'true':'false'}"><div class="r1"><span class="co">${esc(p.company)}</span><span class="age">${esc(age(p))}</span></div><div class="r3"><span class="${stageClass(p.stage)}">${esc(p.stage||'')}</span>${avatar(p.owner)}<span class="own">${esc(p.owner||'')}</span></div></button>`).join('');
    return `<div class="wr"><aside class="wr-side" aria-label="Companies to review"><div class="lh"><span>War room agenda</span><span>${filtered.length} to review</span></div>${rows}</aside><section class="wr-main"><div class="wr-nav"><span class="pos">Card ${idx+1} of ${filtered.length} · <span class="kbd">J</span> next <span class="kbd">K</span> prev <span class="kbd">A</span> approve <span class="kbd">E</span> edit <span class="kbd">I</span> ignore</span><div class="btns"><button type="button" class="pill" data-action="wr-prev" ${idx===0?'disabled':''}>&#8592; Prev</button><button type="button" class="pill primary" data-action="wr-next" ${idx>=filtered.length-1?'disabled':''}>Next card &#8594;</button></div></div>${card(cur)}</section></div>`;
  }
  function syncControls(state, filtered){
    const pending = (state.proposals||[]).filter(p => ['pending','applying'].includes(p.status));
    const sel = $('#owner-filter'); const value = sel ? sel.value : 'all';
    document.querySelectorAll('#owner-seg [data-owner]').forEach(b => { b.classList.toggle('on', b.dataset.owner === value); const c = b.querySelector('[data-cnt]'); if(c) c.textContent = b.dataset.owner === 'all' ? pending.length : pending.filter(p => p.owner === b.dataset.owner).length; });
    document.querySelectorAll('[data-theme-pick]').forEach(b => b.classList.toggle('on', b.dataset.themePick === theme()));
    const today = new Date().toDateString();
    const decided = (state.activity||[]).filter(a => a.at && new Date(a.at).toDateString() === today).length;
    const sp = $('#wr-strip-pending'), sd = $('#wr-strip-decided'); if(sp) sp.textContent = pending.length; if(sd) sd.textContent = decided;
  }
  function rerender(){ const sel = $('#owner-filter'); if(sel) sel.dispatchEvent(new Event('change')); }
  function move(step){ const rows = [...document.querySelectorAll('.wr-row')]; const i = rows.findIndex(r => r.dataset.select === selected); const n = rows[i + step]; if(n){ selected = n.dataset.select; rerender(); } }
  function closeMenus(except){ document.querySelectorAll('.ign-menu.open').forEach(m => { if(m !== except) m.classList.remove('open'); }); }
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-action], [data-owner], [data-theme-pick], [data-mode-pick]'); if(!b) { closeMenus(); return; }
    if(b.dataset.modePick){ const id = b.dataset.modeFor || editingRows; if(id){ modes.set(id, b.dataset.modePick); paintDialogMode(id); drawPreview(); rerender(); } return; }
    if(b.dataset.owner !== undefined){ const sel = $('#owner-filter'); sel.value = b.dataset.owner; selected = null; sel.dispatchEvent(new Event('change')); return; }
    if(b.dataset.themePick){ document.body.dataset.theme = b.dataset.themePick; __STORE_THEME__ rerender(); return; }
    const a = b.dataset.action;
    if(a === 'ns-add'){ rows.push(blankRow()); drawRows(rows.length - 1); syncRows(); return; }
    if(a === 'ns-del'){ rows.splice(Number(b.closest('.ns-row').dataset.row), 1); if(!rows.length) rows.push(blankRow()); drawRows(); syncRows(); return; }
    if(a === 'wr-select'){ selected = b.dataset.select; rerender(); }
    else if(a === 'wr-prev') move(-1);
    else if(a === 'wr-next') move(1);
    else if(a === 'ignore-menu'){ const m = b.parentElement.querySelector('.ign-menu'); closeMenus(m); m.classList.toggle('open'); }
    else if(a !== 'ignore') closeMenus();
  }, true);
  document.addEventListener('keydown', e => {
    if(e.metaKey || e.ctrlKey || e.altKey) return; const t = e.target; if(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return; if(document.querySelector('dialog[open]')) return;
    if(theme() !== 'war-room') return;
    const card = document.querySelector('.wr-main .review-card'); const k = e.key.toLowerCase();
    if(k === 'j') move(1); else if(k === 'k') move(-1);
    else if(k === 'a') card?.querySelector('[data-action=approve]:not(:disabled)')?.click();
    else if(k === 'e') card?.querySelector('[data-action=edit]:not(:disabled)')?.click();
    else if(k === 'i') card?.querySelector('[data-action=ignore-menu]')?.click();
    else return; e.preventDefault();
  });
  try{ const q = new URLSearchParams(location.search).get('theme'); const t = (q === 'focus' || q === 'war-room') ? q : __READ_THEME__; if(t === 'focus' || t === 'war-room') document.body.dataset.theme = t; }catch(_){ }
  // ---- Next Step write mode per card + row editor with a date per step (11/09/2026, Kellogg team).
  // Still presentation only: the page's own approve / decide path sends the bullets; nothing here calls the connector.
  const modes = new Map();
  const mode = id => modes.get(id) === 'append' ? 'append' : 'replace';
  const cellBullets = v => String(v || '').split(/\r?\n/).map(l => l.trim().replace(/^(?:[-*•]\s*)/, '').trim()).filter(Boolean);
  // Keep + add: the bullets already in the Sheet cell, then the new ones (identical lines not repeated).
  function merged(id, bullets, currentCell){ if(mode(id) !== 'append' || !bullets || !bullets.length) return bullets; const cur = cellBullets(currentCell), seen = new Set(cur); return [...cur, ...bullets.filter(x => !seen.has(x))]; }
  function modeControl(id){ const m = mode(id); return `<div class="mode-row"><div class="mode" role="group" aria-label="Next Step write mode"><button type="button" class="${m==='replace'?'on':''}" data-mode-pick="replace" data-mode-for="${esc(id)}" aria-pressed="${m==='replace'}">Replace Next Step</button><button type="button" class="${m==='append'?'on':''}" data-mode-pick="append" data-mode-for="${esc(id)}" aria-pressed="${m==='append'}">Keep current bullets + add these</button></div><span class="mode-hint">${m==='append'?'Bullets already in the Sheet stay; these are added below.':'These replace what is in Next Step now.'}</span></div>`; }
  function paintDialogMode(id){ const m = mode(id); document.querySelectorAll('#edit-mode [data-mode-pick]').forEach(x => { const on = x.dataset.modePick === m; x.classList.toggle('on', on); x.setAttribute('aria-pressed', String(on)); }); }
  const fmtDate = iso => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || ''); return m ? new Date(Date.UTC(+m[1], +m[2]-1, +m[3])).toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric', timeZone:'UTC'}) : ''; };
  const toISO = text => { if(!/\b\d{4}\b/.test(text)) return ''; const t = Date.parse(text + ' UTC'); return Number.isFinite(t) ? new Date(t).toISOString().slice(0, 10) : ''; };
  function splitBullet(bullet){
    let m = /\s*\(complete by ([^)]+)\)\s*$/i.exec(bullet);
    if(m){ const iso = toISO(m[1]); if(iso) return {text: bullet.slice(0, m.index).trim(), date: iso}; }
    m = /\s*[—–-]\s*due (\d{4}-\d{2}-\d{2})\s*$/i.exec(bullet);
    if(m) return {text: bullet.slice(0, m.index).trim(), date: m[1]};
    return {text: bullet, date: ''};
  }
  const joinBullet = (text, date) => { const t = String(text || '').replace(/[\r\n]+/g, ' ').trim(); if(!t) return ''; const d = fmtDate(date); return d ? `${t} (complete by ${d})` : t; };
  let rows = [], editingRows = null;
  const blankRow = () => ({text:'', date:'', t0:null, d0:null});
  // An untouched row goes back exactly as suggested, so its source link survives.
  const rowBullet = r => (r.orig !== undefined && r.text === r.t0 && r.date === r.d0) ? r.orig : joinBullet(r.text, r.date);
  function drawRows(focusIndex){ const box = $('#ns-rows'); if(!box) return; box.innerHTML = rows.map((r, i) => `<div class="ns-row" data-row="${i}"><textarea class="ns-text" rows="2" maxlength="3900" placeholder="Next step" aria-label="Next step ${i+1}">${esc(r.text)}</textarea><input type="date" class="ns-date" value="${esc(r.date)}" aria-label="Complete by date for next step ${i+1}"><button type="button" class="ns-del" data-action="ns-del" aria-label="Remove next step ${i+1}">&times;</button></div>`).join(''); if(focusIndex !== undefined) box.querySelectorAll('.ns-text')[focusIndex]?.focus(); drawPreview(); }
  function drawPreview(){ const out = $('#ns-preview'); if(!out) return; const lines = rows.map(rowBullet).filter(Boolean); out.innerHTML = lines.length ? `<b>${mode(editingRows)==='append'?'Added to Next Step, below the current bullets':'Written to Next Step'}</b>${lines.map(l => esc('- ' + l)).join('\n')}` : '<b>Next Step</b>No rows - Next Step and Last progress stay as they are.'; }
  function syncRows(){ const ta = $('#edit-content'); if(!ta) return; ta.value = rows.map(rowBullet).filter(Boolean).map(x => '- ' + x).join('\n'); ta.dispatchEvent(new Event('input', {bubbles:true})); drawPreview(); }
  // Called by the page right after it fills the hidden #edit-content textarea and opens the dialog.
  function openRows(id){ try{ const ta = $('#edit-content'), box = $('#ns-rows'); if(!ta || !box) return; editingRows = id; rows = cellBullets(ta.value).map(x => { const p = splitBullet(x); return {orig:x, text:p.text, date:p.date, t0:p.text, d0:p.date}; }); if(!rows.length) rows = [blankRow()]; paintDialogMode(id); drawRows(0); }catch(_){ } }
  document.addEventListener('input', e => { const row = e.target.closest?.('.ns-row'); if(!row) return; const r = rows[Number(row.dataset.row)]; if(!r) return; if(e.target.classList.contains('ns-text')) r.text = e.target.value; else if(e.target.classList.contains('ns-date')) r.date = e.target.value; syncRows(); });
  document.addEventListener('keydown', e => { if(e.key === 'Enter' && !e.shiftKey && e.target.closest?.('.ns-row') && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')){ e.preventDefault(); rows.push(blankRow()); drawRows(rows.length - 1); } });
  return {shell, avatar, ownerName, stageClass, shortDate, esc, mode, merged, modeControl, openRows, get selected(){ return selected; }};
})();
"""

# card(p) replacement - valid in both UI scopes (uses displayTrace, bulletList, sourceControls, expandedNextSteps, state, esc from the enclosing scope).
CARD_JS = r"""function card(p){
    const WR=BiancaWarRoom;
    const cellOf=(field)=>{const row=state.rows.find(r=>r.id===p.id)||state.rows.find(r=>r.company===p.company);return row?row[field]:p[field];};
    const applying=p.status==='applying',legacy=p.routingVersion!==undefined&&p.routingVersion!==1,data=displayTrace(p),lineageBlocked=!applying&&!legacy&&!data.available;
    const nextBullets=applying&&p.retry?p.retry.next_step_bullets:(p.nextStepBullets||[]),contextBullets=applying&&p.retry?p.retry.client_context_bullets:(p.clientContextBullets||[]);
    const srcs=(p.sources||[]).slice(0,3);
    const traceNote=(!data.available||![...data.lineage.nextStep,...data.lineage.clientContext].some(edge=>edge.sourceIds.length))?traceMessage(p,data):'';
    const whatsNew=srcs.length?`<ul class="whats-new">${srcs.map(s=>{const line=s.summary||s.subject||'';const ctx=s.quote||s.subject||'';return `<li><details><summary><span>${esc(line)}</span><span class="src">Source: ${esc(sourceType(s.type))}, ${esc(WR.shortDate(s.date))}</span></summary><div class="ctx">${esc(ctx)}</div></details></li>`;}).join('')}</ul>`:'<p class="empty-group">No new information recorded.</p>';
    const open=expandedNextSteps.has(p.id);
    return `<article class="review-card" data-id="${esc(p.id)}" aria-labelledby="title-${esc(p.id)}">
<div class="card-top"><div><h2 class="company-title" id="title-${esc(p.id)}">${esc(p.company)}</h2><div class="meta-bar"><span><span class="k">Stage:</span><span class="${WR.stageClass(p.stage)}">${esc(p.stage||'-')}</span></span><span><span class="k">Owner:</span>${WR.avatar(p.owner)}${esc(WR.ownerName(p.owner))}</span><span><span class="k">Country:</span>${esc(p.country||'-')}</span></div></div></div>
<div class="ns-box"><div class="sec-label ns-label">Suggested next steps</div>${bulletList(p,nextBullets,'nextStep',data)}${!applying&&!legacy&&!lineageBlocked&&nextBullets.length?WR.modeControl(p.id):''}</div>
<div class="sec-label">What's new</div>${whatsNew}
<div class="sec-label">Client context <span class="proposed">Column proposed</span></div>${bulletList(p,contextBullets,'clientContext',data)}
${traceNote?`<p class="approval-note trace-note" role="status">${esc(traceNote)}</p>`:''}${legacy?`<p class="approval-note legacy-note" role="status">${esc(p.error)}</p>`:''}${applying&&!legacy?`<p class="approval-note" role="status">${esc(p.error||'This approval has a saved write request. Retry checks that same request; it will not move the cells twice.')}</p>`:''}
<details class="current-next-steps" data-current-next-steps${open?' open':''}><summary>Current Sheet cells</summary><div class="cells-grid"><div><h4>Next step (now)</h4><div class="current-next-steps-content">${esc(cellOf('nextStep'))||'<i>No Next steps are currently noted.</i>'}</div></div><div><h4>Last progress (now)</h4><div class="current-next-steps-content">${esc(cellOf('lastProgress'))||'<i>No Last progress is currently noted.</i>'}</div></div><div><h4>Client context (now)</h4><div class="current-next-steps-content">${esc(cellOf('clientContext'))||'<i>No Client Context is currently noted.</i>'}</div></div></div></details>
<div class="card-footer"><div class="actions">${legacy?'<button type="button" class="primary" data-review-blocked="true" disabled>Rebuild required</button>':lineageBlocked?'<button type="button" class="primary" data-review-blocked="true" disabled>Rebuild required</button><button type="button" class="quiet" data-action="ignore">Ignore</button>':applying?`<button type="button" class="primary" data-action="retry" ${p.retry?'':'disabled'}>Retry approval<span class="sr-only"> ${esc(p.company)}</span></button>`:`<button type="button" class="primary" data-action="approve"><span class="approve-check" aria-hidden="true">&#10003;</span>Approve<span class="sr-only"> ${esc(p.company)}</span></button><button type="button" class="secondary" data-action="edit">Edit<span class="sr-only"> ${esc(p.company)}</span></button><button type="button" class="quiet" data-action="ignore-menu" aria-haspopup="menu">Ignore &#9662;<span class="sr-only"> options for ${esc(p.company)}</span></button><div class="ign-menu" role="menu"><div class="mh">Ignore options <span class="proposed">Proposed - all dismiss today</span></div><button type="button" class="mi" role="menuitem" data-action="ignore"><b>Not now</b><span>Comes back tomorrow with any new info</span></button><button type="button" class="mi" role="menuitem" data-action="ignore"><b>Ignore this suggestion</b><span>Dismissed for everyone; new info makes a new card</span></button><button type="button" class="mi" role="menuitem" data-action="ignore"><b>Never suggest for this deal</b><span>Mute the company until the stage changes</span></button></div>`}</div><div class="rowlink"><a href="__SHEET_URL__" target="_blank" rel="noopener">Open in Sheet copy &#8599;</a></div></div>
<p class="validation card-error" role="alert" hidden></p></article>`;
  }
"""

def patch(src_html: str, demo: bool) -> str:
    s = src_html
    store = "try{ localStorage.setItem('bianca.review.theme', b.dataset.themePick); }catch(_){ }" if demo else ""
    read = "localStorage.getItem('bianca.review.theme')" if demo else "null"
    # 1. style + fonts
    s = re.sub(r"<style>.*?</style>", lambda m: "<style>" + CSS + "</style>", s, count=1, flags=re.S)
    s = s.replace("<title>", FONTS + "<title>", 1)
    s = s.replace("<body>", '<body data-theme="war-room">', 1)
    # 2. header
    s = re.sub(r"<header class=\"site-header\">.*?</header>", lambda m: HEADER.replace("__LOGO_WHITE__", LOGO_WHITE).replace("__LOGO_BLUE__", LOGO_BLUE).replace("__SHEET_URL__", SHEET_URL), s, count=1, flags=re.S)
    # 3. heading copy
    s = s.replace("Review the latest sales updates, one company at a time.<br>Approve what belongs in your sheet. Edit the details. Skip the rest.",
                  "Review what changed, keep your next steps current.")
    # 4. toolbar
    s = re.sub(r"<div class=\"toolbar\">.*?</div>\n", lambda m: TOOLBAR.replace("__LAYOUT_TOGGLE__", LAYOUT_TOGGLE if demo else "") + "\n", s, count=1, flags=re.S)
    # 5. dialog: proposed controls after the Client Context help, and a Last progress row in the preview
    s = s.replace('<p class="validation" id="edit-error" role="alert" hidden></p><div class="rotation-preview">',
                  DIALOG_EXTRA + '<p class="validation" id="edit-error" role="alert" hidden></p><div class="rotation-preview">', 1)
    s = s.replace('<h3>Current sheet context</h3><div class="rotation-row"><span class="rotation-name">Next Step</span>',
                  '<h3>What will happen in the Sheet copy</h3><div class="rotation-row"><span class="rotation-name">Last progress &#8592;</span><span class="rotation-content">the current Next Step (when Next Step bullets are approved)</span></div><div class="rotation-row"><span class="rotation-name">Next Step (now)</span>', 1)
    s = s.replace('<span>Approve sends actions and blockers to “Next Step,” and needs, signals, and objections to “Client Context.”</span>', '<span>Approve or edit to update Pipeline Next Steps.</span>')
    # 5b. Next Step editor: one row per step (text + date); the original textarea stays as the hidden value the page submits
    assert s.count('<textarea class="editor" id="edit-content"') == 1
    s = s.replace('<textarea class="editor" id="edit-content"', '<textarea class="editor" id="edit-content" hidden', 1)
    s = s.replace('aria-describedby="edit-help edit-error"></textarea><p id="edit-help" class="field-help">One bullet per line. Commitments, next steps, and blockers belong here. Leave empty to keep Next Step and Last progress unchanged.</p>',
                  'aria-describedby="edit-help edit-error"></textarea>' + ROWS_EDITOR + '<p id="edit-help" class="field-help">' + ROWS_HELP + '</p>', 1)
    assert 'id="ns-rows"' in s, "edit-help anchor not found"
    assert s.count("$('#edit-dialog').showModal();$('#edit-content').focus();}") == 1
    s = s.replace("$('#edit-dialog').showModal();$('#edit-content').focus();}", "$('#edit-dialog').showModal();$('#edit-content').focus();BiancaWarRoom.openRows(id);}", 1)
    # 5c. Replace / Keep + add: applied where the page builds the bullets it approves (card Approve and the editor submit)
    if demo:
        cur = "state.rows.find(r=>r.id===ID)?.nextStep"
        a_old = "if(action==='approve')approve(id,state.proposals.find(p=>p.id===id));"
        a_new = "if(action==='approve'){const p=state.proposals.find(p=>p.id===id);if(BiancaWarRoom.mode(id)==='append'&&p.nextStepBullets.length)approve(id,{nextStepBullets:BiancaWarRoom.merged(id,p.nextStepBullets," + cur.replace('ID','id') + "),clientContextBullets:p.clientContextBullets},true);else approve(id,p);}"
        s_old = "nextStepBullets:BiancaCore.parseBullets($('#edit-content').value)"
        s_new = "nextStepBullets:BiancaWarRoom.merged(editingId,BiancaCore.parseBullets($('#edit-content').value)," + cur.replace('ID','editingId') + ")"
    else:
        a_old = "if(action==='approve')decide(id,'approve',state.proposals.find(p=>p.id===id));"
        a_new = "if(action==='approve'){const p=state.proposals.find(p=>p.id===id);if(p&&BiancaWarRoom.mode(id)==='append'&&p.nextStepBullets?.length)decide(id,'approve',{nextStepBullets:BiancaWarRoom.merged(id,p.nextStepBullets,currentNextStep(p)),clientContextBullets:p.clientContextBullets},true);else decide(id,'approve',p);}"
        s_old = "nextStepBullets:BiancaLive.parseBullets($('#edit-content').value)"
        s_new = "nextStepBullets:BiancaWarRoom.merged(editingId,BiancaLive.parseBullets($('#edit-content').value),currentNextStep(state.proposals.find(p=>p.id===editingId)||{}))"
    assert s.count(a_old) == 1 and s.count(s_old) == 1, "approve / submit anchors not found"
    s = s.replace(a_old, a_new, 1).replace(s_old, s_new, 1)
    # 6. footer copy
    s = s.replace("Built alongside your daily brief · Changes require your approval", "Built alongside the daily brief · Approvals write Next Step, Last progress and Client Context only · * synced / sent times are illustrative until the backend exposes them")
    s = s.replace("Built alongside your daily brief · Fictional updates for design review", "Built alongside the daily brief · Demo data: real Nov 3 companies from the Sheet copy, client context illustrative · * times illustrative")
    # 7. owner label
    s = s.replace("const ownerName=code=>({RG:'Ramiro',NC:'Nico',NDL:'Nico'}[code]||code||'Unassigned');", "const ownerName=code=>({RG:'Ramiro',NC:'Nico',NDL:'Nicolás de León'}[code]||code||'Unassigned');")
    s = s.replace("const ownerName=code=>({RG:'Ramiro',NC:'Nico',NDL:'Nico'}[code]||code);", "const ownerName=code=>({RG:'Ramiro',NC:'Nico',NDL:'Nicolás de León'}[code]||code);")
    # 8. source label format
    s = s.replace("label=`${sourceType(source.type)} · ${sourceDate(source.date)}`", "label=`Source: ${sourceType(source.type)}, ${sourceDate(source.date)}`")
    # 9. card() replacement (both files: card ... up to focusNext)
    n_before = s.count("function card(p){")
    s = re.sub(r"function card\(p\)\{.*?(?=\n  function focusNext)", lambda m: CARD_JS.replace("__SHEET_URL__", SHEET_URL).rstrip("\n"), s, count=1, flags=re.S)
    assert n_before == 1, "card() not found exactly once"
    # 10. review list shell
    assert "$('#review-list').innerHTML=filtered.map(card).join('')||" in s, "review-list assignment not found"
    s = s.replace("$('#review-list').innerHTML=filtered.map(card).join('')||", "$('#review-list').innerHTML=BiancaWarRoom.shell(filtered,card,state)||", 1)
    # 11. toast wording
    s = s.replace("approved. Google Sheet updated.", "approved. Written to the Sheet copy.")
    s = s.replace("ignored. The Google Sheet is unchanged.", "ignored. Nothing written to the Sheet copy.")
    s = s.replace("approved. Demo sheet updated.", "approved. Written to the Sheet copy (demo).")
    s = s.replace("ignored. The demo sheet is unchanged.", "ignored. Nothing written (demo).")
    s = s.replace("'Approved · sheet updated'", "'Approved · written to the Sheet copy'").replace("'Edited and approved · sheet updated'", "'Edited and approved · written to the Sheet copy'").replace("'Ignored · no sheet changes'", "'Ignored · nothing written'")
    s = s.replace("'Approved · demo sheet updated'", "'Approved · written to the Sheet copy (demo)'").replace("'Edited and approved · demo sheet updated'", "'Edited and approved · written to the Sheet copy (demo)'")
    # 12. presentation module before the UI script
    marker = '<script id="bianca-live-ui">' if '<script id="bianca-live-ui">' in s else None
    if marker:
        s = s.replace(marker, '<script id="bianca-warroom">' + WR_JS + '</script>\n' + marker, 1)
    else:
        # demo: UI script is the second <script> (no id)
        parts = s.split("<script>")
        assert len(parts) == 2, "demo UI script not found"
        s = parts[0] + '<script id="bianca-warroom">' + WR_JS + '</script>\n<script>' + parts[1]
    if demo:
        seed_js, edges_js = to_js()
        s = re.sub(r"const seed = \[.*?\n  \];", lambda m: seed_js, s, count=1, flags=re.S)
        s = re.sub(r"const seedEdges=\{.*?\n  \};", lambda m: edges_js, s, count=1, flags=re.S)
        # keep the given subject per source (demo originally synthesised one)
        s = s.replace("subject:p.company+' '+(source.type==='Email'?'pilot email':source.type==='Calendar'?'scheduled meeting':'meeting notes')", "subject:source.subject||(p.company+' '+(source.type==='Email'?'email':source.type==='Calendar'?'calendar event':source.type==='Sheet'?'sheet edit':'meeting notes'))")
        s = s.replace("const STORAGE_KEY='bianca.pipeline-review.demo.v3';", "const STORAGE_KEY='bianca.pipeline-review.demo.v4-nov3';")
        s = s.replace("version:'fictional-v1'", "version:'nov3-sheetcopy-v1'")
        s = s.replace("Sample updates · Mon, Nov 2", "Real companies · Sheet copy snapshot Nov 3")
        s = s.replace("<strong>Sample data only.</strong> Actions update a demo sheet in this browser. Your Google Sheet is not connected.", "Approve or edit to update Pipeline Next Steps. <span style=\"color:#8792A8\">Demo: real Nov 3 companies; nothing is written to the Google Sheet.</span>")
    s = s.replace('__STORE_THEME__', store).replace('__READ_THEME__', read)
    assert '__STORE_THEME__' not in s and '__READ_THEME__' not in s
    return s

if __name__ == "__main__":
    src, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    for name, demo in [("sales-pipeline-updates.html", False), ("sales-pipeline-updates-demo.html", True)]:
        html = open(os.path.join(src, name), encoding="utf-8").read()
        patched = patch(html, demo)
        open(os.path.join(out, name), "w", encoding="utf-8").write(patched)
        print(name, len(html), "->", len(patched))
