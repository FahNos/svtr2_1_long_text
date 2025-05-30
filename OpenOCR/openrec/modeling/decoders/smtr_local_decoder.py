import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from torch.nn.init import ones_, trunc_normal_, zeros_

from openrec.modeling.common import DropPath, Identity
from openrec.modeling.decoders.nrtr_decoder import Embeddings
from openrec.modeling.decoders.rctc_decoder import RCTCDecoder

class LocalCrossAttention_rctc(nn.Module):
    def __init__(
        self,
        dim,
        num_heads=1,
        qkv_bias=False,
        qk_scale=None,
        attn_drop=0.0,
        proj_drop=0.0,       
        D=6,
        epsilon=1e-6,
    ):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim**-0.5

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        # Local attention parameters       
        self.D = D
        self.sigma = D / 2.0
        self.Wp = nn.Linear(dim, dim)
        self.vp = nn.Linear(dim, 1)
        self.q_pos = nn.Linear(dim, dim)        

        self.RCTCdecoder = RCTCDecoder(in_channels=384,for_smtr_local=True)

        self.pt_to_question_cross_attn = CrossAttention(
            dim,
            num_heads=2,
            qkv_bias=qkv_bias,
            qk_scale=qk_scale,
            attn_drop=attn_drop,
            proj_drop=0)
        self.normq1 = nn.LayerNorm(dim, eps=epsilon)
        self.normkv1 = nn.LayerNorm(dim, eps=epsilon)

        self.pt_to_image_cross_attn = CrossAttention(
            dim,
            num_heads=2,
            qkv_bias=qkv_bias,
            qk_scale=qk_scale,
            attn_drop=attn_drop,
            proj_drop=0)
        self.normq2 = nn.LayerNorm(dim, eps=epsilon)
        self.normkv2 = nn.LayerNorm(dim, eps=epsilon)
       
    def forward(self, q, kv, sz):
        B, N, C = kv.shape
        # W, C = kv.shape[1:]
        S = q.shape[1]  # [b, S, C]
        bs = q.shape[0]
        H, W = sz
        

        kv = kv.transpose(1, 2).reshape(bs, C, H, W) 
        kv = self.RCTCdecoder(kv) # from [bs, HxW, C] => [bs, W, C]
        
        # Query projection
        q_proj = self.q(q).reshape([-1, S, self.num_heads, C // self.num_heads]).transpose(1, 2)
        q_proj = q_proj * self.scale
        
        # Key/Value projection
        k, v = self.kv(kv).reshape([-1, W, 2, self.num_heads, C // self.num_heads]).permute(2, 0, 3, 1, 4)

        # Local attention implementation
        # Reshape q_proj back for position prediction       
        q_for_pos = q.reshape(bs, S, C) # [bs, S, C]
      
        # Predict center positions
        p_t_init = self.q_pos(q_for_pos)
        p_t_init = self.pt_to_question_cross_attn(               
                    self.normq1(p_t_init), 
                    self.normkv1(q_for_pos),                    
                    )        
        p_t_init = self.pt_to_image_cross_attn(               
                    self.normq2(p_t_init), 
                    self.normkv2(kv),                    
                    )
        p_t = self.vp(torch.tanh(self.Wp(p_t_init)))  # [bs, S , 1]
        p_t = torch.sigmoid(p_t) * W  # scale to [0, W], shape = [bs, S, 1]
        
        # Create Gaussian weights for each head
        indices = torch.arange(W, dtype=torch.float, device=q.device)
        indices = indices.view(1, 1, 1, W).expand(bs, self.num_heads, 1, W)      
        p_expanded = p_t.view(bs, 1, S , 1).expand(bs, self.num_heads, S , W)  
        
        # Calculate Gaussian distribution     
        gauss = torch.exp(-((indices - p_expanded) ** 2) / (2 * (self.sigma ** 2))) # (bs, S , W)
        
        # Standard attention calculation      
        attn = q_proj.matmul(k.transpose(2, 3))  # [b, S, C] @ [b, C, W] = (b, S, W)   
        
        # Apply Gaussian window to attention scores
        attn = attn * gauss # [B, S, W]    
        
        # Softmax normalization
        attn = F.softmax(attn, -1)
        
        if not self.training:
            self.attn_map = attn           
            self.p_t = p_t  
                
        attn = self.attn_drop(attn)

        # Compute context vectors
        x = (attn.matmul(v)).transpose(1, 2).reshape((-1, S, C)) # [b, S, W] @ [b, W, C] = [b, S, C]
        x = self.proj(x)
        x = self.proj_drop(x)
        
        return x
    
class LocalCrossAttention(nn.Module):
    def __init__(
        self,
        dim,
        num_heads=1,
        qkv_bias=False,
        qk_scale=None,
        attn_drop=0.0,
        proj_drop=0.0,       
        D=6,
        epsilon=1e-6,
    ):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim**-0.5

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        # Local attention parameters       
        self.D = D
        self.sigma = D / 2.0
        self.Wp = nn.Linear(dim, dim)
        self.vp = nn.Linear(dim, 1)
        self.q_pos = nn.Linear(dim, dim)

        self.pt_to_question_cross_attn = CrossAttention(
            dim,
            num_heads=2,
            qkv_bias=qkv_bias,
            qk_scale=qk_scale,
            attn_drop=attn_drop,
            proj_drop=0)
        self.normq1 = nn.LayerNorm(dim, eps=epsilon)
        self.normkv1 = nn.LayerNorm(dim, eps=epsilon)

        self.pt_to_image_cross_attn = CrossAttention(
            dim,
            num_heads=2,
            qkv_bias=qkv_bias,
            qk_scale=qk_scale,
            attn_drop=attn_drop,
            proj_drop=0)
        self.normq2 = nn.LayerNorm(dim, eps=epsilon)
        self.normkv2 = nn.LayerNorm(dim, eps=epsilon)
       
    def forward(self, q, kv, sz):
        N, C = kv.shape[1:]
        QN = q.shape[1]
        bs = q.shape[0]
        H, W = sz
        assert N == H * W, f" N ({N}) = h ({H}) * w ({W})."      
        
        # Query projection
        q_proj = self.q(q).reshape([-1, QN, self.num_heads, C // self.num_heads]).transpose(1, 2)
        q_proj = q_proj * self.scale
        
        # Key/Value projection
        k, v = self.kv(kv).reshape([-1, N, 2, self.num_heads, C // self.num_heads]).permute(2, 0, 3, 1, 4)

        # Local attention implementation
        # Reshape q_proj back for position prediction
        q_for_pos = q.reshape(bs, QN, C).repeat(1, H, 1) # [bs, QN * h, C]
      
        # Predict center positions
        p_t_init = self.q_pos(q_for_pos)
        p_t_init = self.pt_to_question_cross_attn(               
                    self.normq1(p_t_init), 
                    self.normkv1(q_for_pos),                    
                    )        
        p_t_init = self.pt_to_image_cross_attn(               
                    self.normq2(p_t_init), 
                    self.normkv2(kv),                    
                    )
        p_t = self.vp(torch.tanh(self.Wp(p_t_init)))  # [bs, S , 1]
        p_t = torch.sigmoid(p_t) * W  # scale to [0, W], shape = [bs, S, 1]
        
        # Create Gaussian weights for each head
        indices = torch.arange(W, dtype=torch.float, device=q.device)
        indices = indices.view(1, 1, 1, W).expand(bs, self.num_heads, 1, W)
        p_expanded = p_t.view(bs, 1, QN * H, 1).expand(bs, self.num_heads, QN * H, W)       
        
        # Calculate Gaussian distribution     
        gauss = torch.exp(-((indices - p_expanded) ** 2) / (2 * (self.sigma ** 2))) # (bs, QN * H, W)
        
        # Standard attention calculation
        attn = q_proj.matmul(k.transpose(2, 3))  # (b, QN, N) => (b, QN, HxW)       
        attn = attn.reshape(bs, -1, QN, H, W).reshape(bs, -1, QN * H, W)
        
        # Apply Gaussian window to attention scores
        attn = attn * gauss       
        attn = attn.reshape(bs, -1, QN, H, W).reshape(bs, -1, QN, N)
        
        # Softmax normalization
        attn = F.softmax(attn, -1)
        
        if not self.training:
            self.attn_map = attn           
            self.p_t = p_t  
                
        attn = self.attn_drop(attn)

        # Compute context vectors
        x = (attn.matmul(v)).transpose(1, 2).reshape((-1, QN, C))
        x = self.proj(x)
        x = self.proj_drop(x)
        
        return x
    
class CrossAttention(nn.Module):

    def __init__(
        self,
        dim,
        num_heads=8,
        qkv_bias=False,
        qk_scale=None,
        attn_drop=0.0,
        proj_drop=0.0,
    ):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim**-0.5

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, q, kv, key_mask=None):
        N, C = kv.shape[1:]
        QN = q.shape[1]
        q = self.q(q).reshape([-1, QN, self.num_heads,
                               C // self.num_heads]).transpose(1, 2)
        q = q * self.scale
        k, v = self.kv(kv).reshape(
            [-1, N, 2, self.num_heads,
             C // self.num_heads]).permute(2, 0, 3, 1, 4)

        attn = q.matmul(k.transpose(2, 3))

        if key_mask is not None:
            attn = attn + key_mask.unsqueeze(1)

        attn = F.softmax(attn, -1)
        if not self.training:
            self.attn_map = attn
        attn = self.attn_drop(attn)

        x = (attn.matmul(v)).transpose(1, 2).reshape((-1, QN, C))
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class SSMatchLayer(nn.Module):

    def __init__(
        self,
        dim,
        nextq2subs_head2=None,
        dynq2img_heads=2,
        mlp_ratio=4.0,
        qkv_bias=False,
        qk_scale=None,
        drop=0.0,
        attn_drop=0.0,
        drop_path=0.0,
        act_layer=nn.GELU,
        epsilon=1e-6,
        use_local=True,
        use_rctc=True,
        D=6
    ):
        super().__init__()
        self.dim = dim
        self.use_local = use_local
        self.use_rctc = use_rctc

        if nextq2subs_head2 is None:
            nextq2subs_head2 = dim // 32
        self.normq1 = nn.LayerNorm(dim, eps=epsilon)
        self.normkv1 = nn.LayerNorm(dim, eps=epsilon)
        self.images_to_question_cross_attn = CrossAttention(
            dim,
            num_heads=nextq2subs_head2,
            qkv_bias=qkv_bias,
            qk_scale=qk_scale,
            attn_drop=attn_drop,
            proj_drop=drop)
        self.normq2 = nn.LayerNorm(dim, eps=epsilon)
        self.normkv2 = nn.LayerNorm(dim, eps=epsilon)     

        if self.use_local:    
            self.question_to_images_cross_attn = LocalCrossAttention_rctc(
                dim,
                num_heads=dynq2img_heads,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                attn_drop=attn_drop,
                proj_drop=drop, 
                D=D)          
        else:
            self.question_to_images_cross_attn = CrossAttention(
                dim,
                num_heads=dynq2img_heads,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                attn_drop=attn_drop,
                proj_drop=drop)      
        
        self.norms = nn.LayerNorm(1, eps=epsilon)
        
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0.0 else Identity()
        self.drop_path2 = DropPath(drop_path) if drop_path > 0.0 else Identity()

    def forward(self, question_f, prompt_f, visual_f, sz, mask=None):       
        H, W = sz
        ratio= W/H

        # question_f shape (b * (max_len_curr + max_len_curr_pre), 1, dim)
        # promp_f shape (b * (max_len_curr + max_len_curr_pre), sub_l + 1, dim)
        question_f = question_f + self.drop_path(
            self.images_to_question_cross_attn(self.normq1(question_f),
                                               self.normkv1(prompt_f), mask))   
        
        question_f = question_f.reshape(visual_f.shape[0], -1, self.dim) # (b , (max_len_curr + max_len_curr_pre), dim) <=> [b, 2, dim]
       
        if self.use_local:         
            question_f = self.question_to_images_cross_attn(               
                self.normq2(question_f), 
                self.normkv2(visual_f),
                sz,
                )
        else:
            question_f = self.question_to_images_cross_attn(                
                self.normq2(question_f), 
                self.normkv2(visual_f)
                )       

        return question_f


class SMTRLocalDecoder(nn.Module):

    def __init__(self,
                 in_channels,
                 out_channels,
                 num_layer=2,
                 nextq2subs_head2=None,
                 dynq2img_heads=2,
                 drop_path_rate=0.1,
                 max_len=25,
                 vis_seq=50,
                 ds=False,
                 pos2d=False,
                 max_size=[8, 32],
                 sub_str_len=5,
                 next_mode=True,
                 infer_aug=False,
                 bi_attn=False,
                 use_local=True,
                 use_rctc=True,
                 D=6,
                 **kwargs):
        super().__init__()

        self.out_channels = out_channels
        dim = in_channels
        self.dim = dim
        self.max_len = max_len + 3  # max_len + eos + bos
        self.char_embed = Embeddings(d_model=dim,
                                     vocab=self.out_channels,
                                     scale_embedding=True)
        self.ignore_index = out_channels - 1
        self.sub_str_len = sub_str_len
        self.bos_next = out_channels - 3
        self.bos_pre = out_channels - 2
        self.eos = 0
        dpr = np.linspace(0, drop_path_rate, num_layer + 2)
        self.next_mode = next_mode
        self.infer_aug = infer_aug
        self.bi_attn = bi_attn
        self.cmff_decoder = nn.ModuleList([
            SSMatchLayer(dim=dim,
                         nextq2subs_head2=nextq2subs_head2,
                         dynq2img_heads=dynq2img_heads,
                         mlp_ratio=4.0,
                         qkv_bias=True,
                         drop_path=dpr[i],
                         use_local=use_local,
                         use_rctc=use_rctc,
                         D=D) for i in range(num_layer)
        ])

        self.ds = ds
        self.pos2d = pos2d
        if not ds:
            self.vis_pos_embed = nn.Parameter(torch.zeros([1, vis_seq, dim],
                                                          dtype=torch.float32),
                                              requires_grad=True)
            trunc_normal_(self.vis_pos_embed, std=0.02)
        elif pos2d:
            pos_embed = torch.zeros([1, max_size[0] * max_size[1], dim],
                                    dtype=torch.float32)
            trunc_normal_(pos_embed, mean=0, std=0.02)
            self.vis_pos_embed = nn.Parameter(pos_embed.transpose(
                1, 2).reshape(1, dim, max_size[0], max_size[1]),
                                              requires_grad=True)

        self.next_token = nn.Parameter(torch.zeros([1, 1, 1, dim],
                                                   dtype=torch.float32),
                                       requires_grad=True)

        self.pre_token = nn.Parameter(torch.zeros([1, 1, 1, dim],
                                                  dtype=torch.float32),
                                      requires_grad=True)

        self.prompt_next_embed = nn.Parameter(torch.zeros(
            [1, 1, self.sub_str_len + 1, dim], dtype=torch.float32),
                                              requires_grad=True)

        self.prompt_pre_embed = nn.Parameter(torch.zeros(
            [1, 1, self.sub_str_len + 1, dim], dtype=torch.float32),
                                             requires_grad=True)

        self.norm_pred = nn.LayerNorm(dim, eps=1e-6)
        self.ques1_head = nn.Linear(dim, self.out_channels - 3)

        trunc_normal_(self.next_token, std=0.02)
        trunc_normal_(self.pre_token, std=0.02)
        trunc_normal_(self.prompt_pre_embed, std=0.02)
        trunc_normal_(self.prompt_next_embed, std=0.02)
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=0.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                zeros_(m.bias)
        elif isinstance(m, nn.LayerNorm):
            zeros_(m.bias)
            ones_(m.weight)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'vis_pos_embed', 'pre_token', 'next_token', 'char_embed'}

    def forward(self, x, sz, data=None):
        if self.training:
            return self.forward_train(x, sz, data)
        else:
            if self.infer_aug:
                if self.bi_attn:
                    return self.forward_test_bi_attn(x, sz)
                return self.forward_test_bi(x, sz)
            return self.forward_test(x, sz)    

    def forward_test_bi(self, x, sz):
       
        if not self.ds:
            visual_f = x + self.vis_pos_embed
        elif self.pos2d:
            visual_f = x + self.vis_pos_embed[:, :, :x.shape[2], :x.shape[3]]
            visual_f = x.flatten(2).transpose(1, 2)
        else:
            visual_f = x
        bs = x.shape[0]       
       
        next = self.next_token.tile([bs, 1, 1, 1]).squeeze(1)  # [bs, 1, 1, dim] => [bs, 1, dim] -  max_len_curr = 1
        pre = self.pre_token.tile([bs, 1, 1, 1]).squeeze(1)    # [bs, 1, dim] -  max_len_curr_pre = 1
        next_pre = torch.concat([next, pre], 0)       # => [bs*2, 1, dim]       

        
        prompt_next_embed = self.prompt_next_embed.tile([bs, 1, 1, 1]).squeeze(1)  # [bs, sub_str_len+1, dim]
        prompt_pre_embed = self.prompt_pre_embed.tile([bs, 1, 1, 1]).squeeze(1)    # [bs, sub_str_len+1, dim]

        # subs
        next_id = torch.full([bs, self.sub_str_len],    # subs - targer[1] 
                             self.bos_next,
                             dtype=torch.long,
                             device=x.device)
        pre_id = torch.full([bs, self.sub_str_len],     #subs - target[4] 
                            self.bos_pre,
                            dtype=torch.long,
                            device=x.device)
       
        next_pred_id_list = torch.full([bs, self.max_len],
                                        self.ignore_index,
                                        dtype=torch.long,
                                        device=x.device)
        pre_pred_id_list = torch.full([bs, self.max_len],
                                       self.ignore_index,
                                       dtype=torch.long,
                                       device=x.device)
        next_logits_all = []
        pre_logits_all = []

        mask_pad = torch.zeros([bs*2, 1],
                                   dtype=torch.float32,
                                   device=x.device)

        for j in range(0, min(70, self.max_len - 1)):

            # prompt_char  [bs, sub_str_len+1, dim]
            prompt_char_next = torch.concat([
                prompt_next_embed[:, :1, :],    # [bs,  1, dim]
                prompt_next_embed[:, 1:, :] + self.char_embed(next_id) # [bs, sub_l, dim] + [bs, sub_l, dim]
            ], 1)  # [bs, sub_str_len+1, dim]

            prompt_char_pre = torch.concat([
                prompt_pre_embed[:, :1, :],
                prompt_pre_embed[:, 1:, :] + self.char_embed(pre_id)
            ], 1)  # [bs, sub_str_len+1, dim]
          
            prompt_char = torch.concat([prompt_char_next, prompt_char_pre], 0)  # [bs*2, sub_l+1, dim]
            
            # mask
            mask_next = torch.where(next_id == self.bos_next, float('-inf'), 0) # [bs, sub_l]
            mask_pre = torch.where(pre_id == self.bos_pre, float('-inf'), 0)    # [bs, sub_l]   

            mask = torch.concat([mask_next, mask_pre], 0)  # [bs*2, sub_l]

            
            pred_token = next_pre  #  [bs*2, 1, dim]            
            prompt_char = prompt_char #   [bs*2, sub_l + 1, dim]
            visual_f_i = visual_f   # (bs, N, dim)
            mask = torch.concat([mask_pad, mask], 1)    # [bs*2, subl + 1]
          

            for layer in self.cmff_decoder:
                pred_token = layer(
                    pred_token, 
                    prompt_char, 
                    visual_f_i,
                    sz,
                    mask.unsqueeze(1)
                    )
       
            logits_next_i = self.ques1_head(self.norm_pred(pred_token)) #(bs, 2, out_channel - 3)           
            logits = F.softmax(logits_next_i, -1)   #(bs, 2, out_channel - 3)   
            pred_id_i = logits.argmax(-1)  # [bs, 2]  
                     
            
            next_pred_id_list[:, j:j + 1] = pred_id_i[:, :1]
            pre_pred_id_list[:, j:j + 1] = pred_id_i[:, 1:2]

            if not (next_pred_id_list == self.eos).any(dim=-1).all():
                next_logits_all.append(logits[:, :1])               
                next_id = torch.concat([next_id[:, 1:], pred_id_i[:, :1]], 1)

            if not (pre_pred_id_list == self.eos).any(dim=-1).all():
                pre_logits_all.append(logits[:, 1:2])
                pre_id = torch.concat([pred_id_i[:, 1:2], pre_id[:, :-1]], 1)

            if (next_pred_id_list == self.eos).any(dim=-1).all() and (
                    pre_pred_id_list == self.eos).any(dim=-1).all():
                break      

        if len(next_logits_all) > self.sub_str_len and len(pre_logits_all) > self.sub_str_len:
            next_logits_all_ = torch.concat(next_logits_all[:-1], 1)  # [bs, l]
            pre_logits_all_ = torch.concat(pre_logits_all[:-1][::-1], 1)  # [bs, l]

            next_id = next_logits_all_.argmax(-1)[:, -self.sub_str_len:]
            pre_id = pre_logits_all_.argmax(-1)[:, :self.sub_str_len]
            next_logits_all = []
            ques_next = self.next_token.tile([bs, 1, 1, 1]).squeeze(1)

            mask_pad = torch.zeros([bs, 1],
                                   dtype=torch.float32,
                                   device=x.device)
            for j in range(0, min(70, self.max_len - 1)):

                prompt_next = torch.concat([                  
                    prompt_next_embed[:, :1, :],
                    prompt_next_embed[:, 1:, :] + self.char_embed(next_id)
                ], 1)  # [bs, sub_str_len+1, dim]

                mask_next = torch.where(next_id == self.bos_next,
                                        float('-inf'), 0)  # [bs, sub_str_len]
                
                mask = torch.concat([mask_pad, mask_next], 1)   # [bs, sub_l + 1]               
                ques_next_i = ques_next     # [bs, 1, dim]
                visual_f_i = visual_f  

                for layer in self.cmff_decoder:
                    ques_next_i = layer(
                        ques_next_i, 
                        prompt_next,
                        visual_f_i, 
                        sz, 
                        mask.unsqueeze(1)
                        )
                    
                logits_next_i = self.ques1_head(
                    self.norm_pred(ques_next_i)
                    )
                logits = F.softmax(logits_next_i, -1)
                pred_id_i = logits.argmax(-1)   # [bs,1]

                next_logits_all.append(logits)
                next_id = torch.concat([next_id[:, 1:], pred_id_i], 1)
                if next_id.equal(pre_id):
                    break
            next_logits_all = torch.concat(next_logits_all, 1)
            next_logits_all_ = torch.concat(
                [next_logits_all_, next_logits_all], 1
                )

            return torch.concat(
                [next_logits_all_, 
                pre_logits_all_[:, self.sub_str_len:]],
                1)
        else:
            return torch.concat(next_logits_all + pre_logits_all[::-1], 1)


    def forward_test_bi_attn(self, x, sz):
        self.attn_maps = []
        if not self.ds:
            visual_f = x + self.vis_pos_embed
        elif self.pos2d:
            visual_f = x + self.vis_pos_embed[:, :, :x.shape[2], :x.shape[3]]
            visual_f = x.flatten(2).transpose(1, 2)
        else:
            visual_f = x
        bs = 2
        if 1:
            next = self.next_token
            pre = self.pre_token
            next_pre = torch.concat([next, pre], 0)
            next_pre = next_pre.squeeze(1) #2, 1, dim

            prompt_next_embed = self.prompt_next_embed.squeeze(1)
            prompt_pre_embed = self.prompt_pre_embed.squeeze(1)

            next_id = torch.full([1, self.sub_str_len], self.bos_next, dtype=torch.long, device=x.device)
            pre_id = torch.full([1, self.sub_str_len], self.bos_pre, dtype=torch.long, device=x.device)
            # prompt_next_bos = self.char_embed(prompt_id)
            # pred_prob_list = torch.full([bs, self.sub_str_len], self.ignore_index, dtype=torch.long, device=x.device)
            next_pred_id_list = torch.full([1, self.max_len], self.ignore_index, dtype=torch.long, device=x.device)
            pre_pred_id_list = torch.full([1, self.max_len], self.ignore_index, dtype=torch.long, device=x.device)
            next_logits_all = []
            pre_logits_all = []
            attn_map_next = []
            attn_map_pre = []
            mask_pad = torch.zeros([bs, 1], dtype=torch.float32, device=x.device)
            for j in range(0, min(70, self.max_len-1)):
                
                prompt_char_next = torch.concat([prompt_next_embed[:, :1, :], prompt_next_embed[:, 1:, :] + self.char_embed(next_id)], 1) # b, sub_l, dim
                prompt_char_pre = torch.concat([prompt_pre_embed[:, :1, :], prompt_pre_embed[:, 1:, :] + self.char_embed(pre_id)], 1) # b, sub_l, dim
                prompt_char = torch.concat([prompt_char_next, prompt_char_pre], 0) #2, 6, dim
                # prompt_char = prompt_char.flatten(0, 1)

                mask_next = torch.where(next_id == self.bos_next, float('-inf'), 0) # b, subs_l
                mask_pre = torch.where(pre_id == self.bos_pre, float('-inf'), 0) # b, subs_l
                mask = torch.concat([mask_next, mask_pre], 0) #2, 5
                mask = torch.concat([mask_pad, mask], 1) # 2, 6
                pred_token = next_pre
                visual_f_i = visual_f[:2] # 2 l dim
                for layer in self.cmff_decoder:
                    pred_token = layer(pred_token, prompt_char, visual_f_i, sz, mask.unsqueeze(1))
                
                
                logits_next_i = self.ques1_head(self.norm_pred(pred_token))
                logits = F.softmax(logits_next_i, -1)
                pred_id_i = logits.argmax(-1) #2, 1
                # print(pred_id_i.shape)
                
                next_pred_id_list[:, j:j+1] = pred_id_i[:1]
                pre_pred_id_list[:, j:j+1] = pred_id_i[1:2]
                if not (next_pred_id_list == self.eos).any(dim=-1).all():
                    next_logits_all.append(logits[:1])
                    attn_map_next.append(self.cmff_decoder[-1].question_to_images_cross_attn.attn_map[0])
                    next_id = torch.concat([next_id[:, 1:], pred_id_i[:1]], 1)
                if not (pre_pred_id_list == self.eos).any(dim=-1).all():
                    pre_logits_all.append(logits[1:2])
                    attn_map_pre.append(self.cmff_decoder[-1].question_to_images_cross_attn.attn_map[1])
                    pre_id = torch.concat([pred_id_i[1:2], pre_id[:, :-1]], 1)
                
                if (next_pred_id_list == self.eos).any(dim=-1).all() and (pre_pred_id_list == self.eos).any(dim=-1).all():
                    break
                # print(next_id, pre_id)
            # exit(0)
            if len(next_logits_all) > self.sub_str_len and len(pre_logits_all) > self.sub_str_len:
                next_logits_all_ = torch.concat(next_logits_all[:-1], 1) # 1, l
                pre_logits_all_ = torch.concat(pre_logits_all[:-1][::-1], 1) #1, l

                next_id = next_logits_all_.argmax(-1)[:, -self.sub_str_len:]
                pre_id = pre_logits_all_.argmax(-1)[:, :self.sub_str_len]
                next_logits_all_mid = []
                attn_map_next_mid = []
                ques_next = self.next_token.tile([1, 1, 1, 1]).squeeze(1)
                mask_pad = torch.zeros([1, 1], dtype=torch.float32, device=x.device)
                for j in range(0, min(70, self.max_len-1)):
                    
                    prompt_next = torch.concat([prompt_next_embed[:, :1, :], prompt_next_embed[:, 1:, :] + self.char_embed(next_id)], 1) # b, sub_l, dim
                    mask_next = torch.where(next_id == self.bos_next, float('-inf'), 0) # b, subs_l
                    mask = torch.concat([mask_pad, mask_next], 1)
                    # prompt_next = self.char_embed(prompt_id)
                    ques_next_i = ques_next
                    visual_f_i = visual_f[2:3]
                    for layer in self.cmff_decoder:
                        ques_next_i = layer(ques_next_i, prompt_next, visual_f_i, sz, mask.unsqueeze(1))
                    logits_next_i = self.ques1_head(self.norm_pred(ques_next_i))
                    attn_map_next_mid.append(self.cmff_decoder[-1].question_to_images_cross_attn.attn_map[0])
                    logits = F.softmax(logits_next_i, -1)
                    pred_id_i = logits.argmax(-1)
                    next_logits_all_mid.append(logits)
                    next_id = torch.concat([next_id[:, 1:, ], pred_id_i], 1)
                    if next_id.equal(pre_id):
                        break
                next_logits_all_mid = torch.concat(next_logits_all_mid, 1)
                # next_logits_all_ = torch.concat([next_logits_all_, next_logits_all], 1)
                self.attn_maps = [attn_map_next, attn_map_next_mid, attn_map_pre[::-1]]
                return [torch.concat(next_logits_all, 1), next_logits_all_mid, torch.concat(pre_logits_all[::-1], 1)]
            else:
                self.attn_maps = [attn_map_next, attn_map_pre[::-1]]
                return [torch.concat(next_logits_all, 1), torch.concat(pre_logits_all[::-1], 1)]


    def forward_test(self, x, sz):
        self.attn_maps = []
        if not self.ds:
            visual_f = x + self.vis_pos_embed
        elif self.pos2d:
            visual_f = x + self.vis_pos_embed[:, :, :x.shape[2], :x.shape[3]]
            visual_f = x.flatten(2).transpose(1, 2)
        else:
            visual_f = x
        bs = x.shape[0]

        if self.next_mode:
            ques_next = self.next_token.tile([bs, 1, 1, 1]).squeeze(1) #[b, 1, dim]
            prompt_next_embed = self.prompt_next_embed.tile([bs, 1, 1,
                                                             1]).squeeze(1)
            prompt_id = torch.full([bs, self.sub_str_len],
                                   self.bos_next,
                                   dtype=torch.long,
                                   device=x.device)
            pred_id_list = torch.full([bs, self.max_len],
                                      self.ignore_index,
                                      dtype=torch.long,
                                      device=x.device)
            logits_all = []
            mask_pad = torch.zeros([bs, 1],
                                   dtype=torch.float32,
                                   device=x.device)
            for j in range(0, self.max_len - 1):

                prompt_next = torch.concat([
                    prompt_next_embed[:, :1, :],
                    prompt_next_embed[:, 1:, :] + self.char_embed(prompt_id)
                ], 1)  # b, sub_l, dim
                mask_next = torch.where(prompt_id == self.bos_next,
                                        float('-inf'), 0)  # b, subs_l
                
                mask = torch.concat([mask_pad, mask_next], 1) #[bs, 1] concat [bs, sub_l]
                ques_next_i = ques_next #[b, 1, dim]
                visual_f_i = visual_f

                for layer in self.cmff_decoder:
                    ques_next_i = layer(
                        ques_next_i, 
                        prompt_next, 
                        visual_f_i, 
                        sz,  
                        mask.unsqueeze(1)
                        )
                    
                self.attn_maps.append(
                    self.cmff_decoder[-1].question_to_images_cross_attn.
                    attn_map[0])
                logits_next_i = self.ques1_head(self.norm_pred(ques_next_i)) #(bs, 1, dim)
                logits = F.softmax(logits_next_i, -1)
                pred_id_i = logits.argmax(-1)   #(bs, 1, 1)

                logits_all.append(logits)
                pred_id_list[:, j:j + 1] = pred_id_i
                if (pred_id_list == self.eos).any(dim=-1).all():
                    break
                prompt_id = torch.concat(
                    [
                        prompt_id[:, 1:, ],
                        pred_id_i,
                    ],
                    1,
                )
            return torch.concat(logits_all, 1)
        else:
            ques_next = self.pre_token.tile([bs, 1, 1, 1]).squeeze(1)
            prompt_pre_embed = self.prompt_pre_embed.tile([bs, 1, 1,
                                                           1]).squeeze(1)
            prompt_id = torch.full([bs, self.sub_str_len],
                                   self.bos_pre,
                                   dtype=torch.long,
                                   device=x.device)
            pred_id_list = torch.full([bs, self.max_len],
                                      self.ignore_index,
                                      dtype=torch.long,
                                      device=x.device)
            logits_all = []
            mask_pad = torch.zeros([bs, 1],
                                   dtype=torch.float32,
                                   device=x.device)
            for j in range(0, self.max_len - 1):

                prompt_next = torch.concat([
                    prompt_pre_embed[:, :1, :],
                    prompt_pre_embed[:, 1:, :] + self.char_embed(prompt_id)
                ], 1)  # b, sub_l, dim
                mask_next = torch.where(prompt_id == self.bos_pre,
                                        float('-inf'), 0)  # b, subs_l
                mask = torch.concat([mask_pad, mask_next], 1)
                ques_next_i = ques_next
                visual_f_i = visual_f
                for layer in self.cmff_decoder:
                    ques_next_i = layer(ques_next_i, prompt_next, visual_f_i, sz,
                                        mask.unsqueeze(1))
                logits_next_i = self.ques1_head(self.norm_pred(ques_next_i))
                logits = F.softmax(logits_next_i, -1)
                pred_id_i = logits.argmax(-1)
                logits_all.append(logits)
                pred_id_list[:, j:j + 1] = pred_id_i
                if (pred_id_list == self.eos).any(dim=-1).all():
                    break
                prompt_id = torch.concat(
                    [
                        pred_id_i,
                        prompt_id[:, :-1, ],
                    ],
                    1,
                )
            return torch.concat(logits_all, 1)

    def forward_train(self, x, sz, targets=None):
        bs = x.shape[0]

        if not self.ds:
            visual_f = x + self.vis_pos_embed
        elif self.pos2d:
            visual_f = x + self.vis_pos_embed[:, :, :x.shape[2], :x.shape[3]]
        else:
            visual_f = x
        max_len_curr = targets[3].max()
      
        subs = targets[1][:, :max_len_curr, :]  # b, n, subs_l
        mask_next = torch.where(subs == self.bos_next, float('-inf'),
                                0)  # b, n, subs_l
        prompt_next_embed = self.prompt_next_embed.tile(
            [bs, max_len_curr, 1, 1])
        prompt_char_next = torch.concat([
            prompt_next_embed[:, :, :1, :],
            prompt_next_embed[:, :, 1:, :] + self.char_embed(subs)
        ], 2)  # b, n, sub_l, dim
        next = self.next_token.tile([bs, max_len_curr, 1, 1])

        max_len_curr_pre = targets[6].max()
       
        subs = targets[4][:, :max_len_curr_pre, :]  # b, n, subs_l
        mask_pre = torch.where(subs == self.bos_pre, float('-inf'),
                               0)  # b, n, subs_l
        prompt_pre_embed = self.prompt_pre_embed.tile(
            [bs, max_len_curr_pre, 1, 1])
        prompt_char_pre = torch.concat([
            prompt_pre_embed[:, :, :1, :],
            prompt_pre_embed[:, :, 1:, :] + self.char_embed(subs)
        ], 2)  # b, n, sub_l, dim
        pre = self.pre_token.tile([bs, max_len_curr_pre, 1, 1])  # b, n, 1, dim

        prompt_char = torch.concat([prompt_char_next, prompt_char_pre], 1)
        next_pre = torch.concat([next, pre], 1)

        mask_pad = torch.zeros([bs * (max_len_curr + max_len_curr_pre), 1],
                               dtype=torch.float32,
                               device=x.device)
        mask = torch.concat([mask_next, mask_pre], 1).flatten(0, 1)
        mask = torch.concat([mask_pad, mask], 1)
        next_pre = next_pre.flatten(0, 1)
        prompt_char = prompt_char.flatten(0, 1)
        for layer in self.cmff_decoder:
            next_pre = layer(next_pre, prompt_char, visual_f, sz,
                             mask.unsqueeze(1))

        answer1_pred = self.ques1_head(self.norm_pred(next_pre))
        logits = answer1_pred[:, :max_len_curr]

        label = torch.concat(
            [targets[2][:, :max_len_curr], targets[5][:, :max_len_curr_pre]],
            1)
        loss1 = F.cross_entropy(answer1_pred.flatten(0, 1),
                                label.flatten(0, 1),
                                ignore_index=self.ignore_index,
                                reduction='mean')
        loss = {'loss': loss1}
        return [loss, logits]