import logging
import random
import pandas as pd
from faker import Faker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def generate_mock_ticket_data_chunked(
    total_records: int, 
    abuse_ratio: float, 
    output_file: str,
    abuse_pool_size: int = 20,
    chunk_size: int = 10000
) -> None:
    """
    어뷰징 탐지 시뮬레이션을 위한 예매 모의 데이터를 생성합니다.
    메모리 초과를 방지하기 위해 Chunk 단위로 나누어 CSV에 바로 병합합니다.
    
    Args:
        total_records: 생성할 전체 예매 데이터 건수
        abuse_ratio: 전체 데이터 중 어뷰징(중복) 데이터가 차지하는 비율
        output_file: 결과물을 저장할 CSV 파일 경로
        abuse_pool_size: 어뷰징 패턴을 형성할 고정된 배송지 및 결제수단 풀의 크기
        chunk_size: 한 번에 메모리에 올릴 데이터 건수 (기본값 1만 건)
    """

    if total_records <= 0 or abuse_pool_size <= 0 or chunk_size <= 0:
        raise ValueError("생성 건수, 풀 사이즈, 청크 사이즈는 0보다 커야 합니다.")
    if not (0.0 <= abuse_ratio <= 1.0):
        raise ValueError("어뷰징 비율은 0.0과 1.0 사이여야 합니다.")
    
    fake = Faker('ko_KR')
    
    logger.info(f"어뷰징 패턴 생성을 위한 고정 풀 구성 중 (크기: {abuse_pool_size})")
    abuse_address_pool = [fake.address() for _ in range(abuse_pool_size)]
    abuse_payment_pool = [fake.credit_card_number() for _ in range(abuse_pool_size)]

    # 정확한 비율 검증을 위해 어뷰징 데이터 인덱스 사전 추출
    abuse_count = int(total_records * abuse_ratio)
    abuse_indices = set(random.sample(range(1, total_records + 1), abuse_count))
    
    logger.info("모의 예매 데이터 병합 생성을 시작합니다 (메모리 초과 방지 Chunk 처리 적용).")
    
    chunk_data = []
    for index in range(1, total_records + 1):
        # 사전 추출된 인덱스 셋(Set)을 검사하여 어뷰징 데이터 판별
        is_abuse_target = index in abuse_indices
        
        target_address = random.choice(abuse_address_pool) if is_abuse_target else fake.address()
        target_payment = random.choice(abuse_payment_pool) if is_abuse_target else fake.credit_card_number()
        
        chunk_data.append({
            'account_id': fake.uuid4(),
            'name': fake.name(),
            'phone': fake.phone_number(),
            'address': target_address,
            'payment_method': target_payment,
            'ip': fake.ipv4()
        })
        
        if index % chunk_size == 0 or index == total_records:
            df_chunk = pd.DataFrame(chunk_data)
            
            # 첫 번째 청크일 때만 새로 작성하고 헤더를 추가, 이후부터는 이어쓰기
            write_mode = 'w' if index <= chunk_size else 'a'
            write_header = True if index <= chunk_size else False
            
            df_chunk.to_csv(output_file, mode=write_mode, header=write_header, index=False, encoding='utf-8-sig')
            logger.info(f"데이터 생성 진행률: {index:,} / {total_records:,} 건 완료")
            
            # 파일 기록 후 리스트를 초기화하여 메모리 반환
            chunk_data = []