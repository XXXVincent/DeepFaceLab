import numpy as np
import cv2

from nnlib import DSSIMMaskLossClass
from nnlib import conv
from nnlib import upscale
from models import ModelBase
from facelib import FaceType
from samples import *

class Model(ModelBase):

    encoderH5 = 'encoder.h5'
    decoder_srcH5 = 'decoder_src.h5'
    decoder_dstH5 = 'decoder_dst.h5'

    #override
    def onInitialize(self, **in_options):
        tf = self.tf
        keras = self.keras
        K = keras.backend
        self.set_vram_batch_requirements( {2.5:2,3:2,4:2,4:4,5:8,6:8,7:16,8:16,9:24,10:24,11:32,12:32,13:48} )
                
        bgr_shape, mask_shape, self.encoder, self.decoder_src, self.decoder_dst = self.Build(self.created_vram_gb)
        if not self.is_first_run():
            self.encoder.load_weights     (self.get_strpath_storage_for_file(self.encoderH5))
            self.decoder_src.load_weights (self.get_strpath_storage_for_file(self.decoder_srcH5))
            self.decoder_dst.load_weights (self.get_strpath_storage_for_file(self.decoder_dstH5))
            
        input_src_bgr = self.keras.layers.Input(bgr_shape)
        input_src_mask = self.keras.layers.Input(mask_shape)
        input_dst_bgr = self.keras.layers.Input(bgr_shape)
        input_dst_mask = self.keras.layers.Input(mask_shape)

        rec_src_bgr, rec_src_mask = self.decoder_src( self.encoder(input_src_bgr) )        
        rec_dst_bgr, rec_dst_mask = self.decoder_dst( self.encoder(input_dst_bgr) )

        self.ae = self.keras.models.Model([input_src_bgr,input_src_mask,input_dst_bgr,input_dst_mask], [rec_src_bgr, rec_src_mask, rec_dst_bgr, rec_dst_mask] )
            
        if self.is_training_mode:
            self.ae, = self.to_multi_gpu_model_if_possible ( [self.ae,] )

        self.ae.compile(optimizer=self.keras.optimizers.Adam(lr=5e-5, beta_1=0.5, beta_2=0.999),
                        loss=[ DSSIMMaskLossClass(self.tf)([input_src_mask]), 'mae', DSSIMMaskLossClass(self.tf)([input_dst_mask]), 'mae' ] )
  
        self.src_view = K.function([input_src_bgr],[rec_src_bgr, rec_src_mask])
        self.dst_view = K.function([input_dst_bgr],[rec_dst_bgr, rec_dst_mask])
        
        if self.is_training_mode:
            f = SampleProcessor.TypeFlags
            self.set_training_data_generators ([            
                    SampleGeneratorFace(self.training_data_src_path, debug=self.is_debug(), batch_size=self.batch_size, 
                            output_sample_types=[ [f.WARPED_TRANSFORMED | f.FACE_ALIGN_HALF | f.MODE_BGR, 128], 
                                                  [f.TRANSFORMED | f.FACE_ALIGN_HALF | f.MODE_BGR, 128], 
                                                  [f.TRANSFORMED | f.FACE_ALIGN_HALF | f.MODE_M | f.FACE_MASK_FULL, 128] ] ),
                                                  
                    SampleGeneratorFace(self.training_data_dst_path, debug=self.is_debug(), batch_size=self.batch_size, 
                            output_sample_types=[ [f.WARPED_TRANSFORMED | f.FACE_ALIGN_HALF | f.MODE_BGR, 128], 
                                                  [f.TRANSFORMED | f.FACE_ALIGN_HALF | f.MODE_BGR, 128], 
                                                  [f.TRANSFORMED | f.FACE_ALIGN_HALF | f.MODE_M | f.FACE_MASK_FULL, 128] ] )
                ])
            
    #override
    def onSave(self):        
        self.save_weights_safe( [[self.encoder, self.get_strpath_storage_for_file(self.encoderH5)],
                                [self.decoder_src, self.get_strpath_storage_for_file(self.decoder_srcH5)],
                                [self.decoder_dst, self.get_strpath_storage_for_file(self.decoder_dstH5)]])
        
    #override
    def onTrainOneEpoch(self, sample):
        warped_src, target_src, target_src_mask = sample[0]
        warped_dst, target_dst, target_dst_mask = sample[1]    

        total, loss_src_bgr, loss_src_mask, loss_dst_bgr, loss_dst_mask = self.ae.train_on_batch( [warped_src, target_src_mask, warped_dst, target_dst_mask], [target_src, target_src_mask, target_dst, target_dst_mask] )

        return ( ('loss_src', loss_src_bgr), ('loss_dst', loss_dst_bgr) )
        
    #override
    def onGetPreview(self, sample):
        test_A   = sample[0][1][0:4] #first 4 samples
        test_A_m = sample[0][2][0:4] #first 4 samples
        test_B   = sample[1][1][0:4]
        test_B_m = sample[1][2][0:4]
        
        AA, mAA = self.src_view([test_A])                                       
        AB, mAB = self.src_view([test_B])
        BB, mBB = self.dst_view([test_B])
        
        mAA = np.repeat ( mAA, (3,), -1)
        mAB = np.repeat ( mAB, (3,), -1)
        mBB = np.repeat ( mBB, (3,), -1)
        
        st = []
        for i in range(0, len(test_A)):
            st.append ( np.concatenate ( (
                test_A[i,:,:,0:3],
                AA[i],
                #mAA[i],
                test_B[i,:,:,0:3], 
                BB[i], 
                #mBB[i],                
                AB[i],
                #mAB[i]
                ), axis=1) )
            
        return [ ('H128', np.concatenate ( st, axis=0 ) ) ]

    def predictor_func (self, face):        
        face_128_bgr = face[...,0:3]
        face_128_mask = np.expand_dims(face[...,3],-1)
        
        x, mx = self.src_view ( [ np.expand_dims(face_128_bgr,0) ] )
        x, mx = x[0], mx[0]     
        
        return np.concatenate ( (x,mx), -1 )

    #override
    def get_converter(self, **in_options):
        from models import ConverterMasked
        
        if 'erode_mask_modifier' not in in_options.keys():
            in_options['erode_mask_modifier'] = 0
        in_options['erode_mask_modifier'] += 100
            
        if 'blur_mask_modifier' not in in_options.keys():
            in_options['blur_mask_modifier'] = 0
        in_options['blur_mask_modifier'] += 100
        
        return ConverterMasked(self.predictor_func, predictor_input_size=128, output_size=128, face_type=FaceType.HALF, **in_options)
    
    def Build(self, created_vram_gb):
        bgr_shape = (128, 128, 3)
        mask_shape = (128, 128, 1)
        
        def Encoder(input_shape):
            input_layer = self.keras.layers.Input(input_shape)
            x = input_layer
            if created_vram_gb >= 5:
                x = conv(self.keras, x, 128)
                x = conv(self.keras, x, 256)
                x = conv(self.keras, x, 512)
                x = conv(self.keras, x, 1024)
                x = self.keras.layers.Dense(512)(self.keras.layers.Flatten()(x))
                x = self.keras.layers.Dense(8 * 8 * 512)(x)
                x = self.keras.layers.Reshape((8, 8, 512))(x)
                x = upscale(self.keras, x, 512)
            else:
                x = conv(self.keras, x, 128)
                x = conv(self.keras, x, 256)
                x = conv(self.keras, x, 512)
                x = conv(self.keras, x, 1024)
                x = self.keras.layers.Dense(256)(self.keras.layers.Flatten()(x))
                x = self.keras.layers.Dense(8 * 8 * 256)(x)
                x = self.keras.layers.Reshape((8, 8, 256))(x)
                x = upscale(self.keras, x, 256)
                
            return self.keras.models.Model(input_layer, x)

        def Decoder():
            if created_vram_gb >= 5:
                input_ = self.keras.layers.Input(shape=(16, 16, 512))
                x = input_
                x = upscale(self.keras, x, 512)
                x = upscale(self.keras, x, 256)
                x = upscale(self.keras, x, 128)
                
                y = input_  #mask decoder
                y = upscale(self.keras, y, 512)
                y = upscale(self.keras, y, 256)
                y = upscale(self.keras, y, 128)
            else:
                input_ = self.keras.layers.Input(shape=(16, 16, 256))
                x = input_
                x = upscale(self.keras, x, 256)
                x = upscale(self.keras, x, 128)
                x = upscale(self.keras, x, 64)
                
                y = input_  #mask decoder
                y = upscale(self.keras, y, 256)
                y = upscale(self.keras, y, 128)
                y = upscale(self.keras, y, 64)
            
            x = self.keras.layers.convolutional.Conv2D(3, kernel_size=5, padding='same', activation='sigmoid')(x)
            y = self.keras.layers.convolutional.Conv2D(1, kernel_size=5, padding='same', activation='sigmoid')(y)
            
            
            return self.keras.models.Model(input_, [x,y])
            
        return bgr_shape, mask_shape, Encoder(bgr_shape), Decoder(), Decoder()
