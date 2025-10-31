# Example data visualization script
import pandas as pd
import matplotlib.pyplot as plt

# Read patient demographics data
try:
    with visualization.open('patients/demographics.csv', 'r') as f:
        df_demo = pd.read_csv(f)
    
    # Plot 1: Age Distribution
    plt.figure(figsize=(10, 6))
    plt.hist(df_demo['age'], bins=15, alpha=0.7, color='skyblue', edgecolor='black')
    plt.title('Age Distribution of Patients')
    plt.xlabel('Age')
    plt.ylabel('Frequency')
    plt.grid(True, alpha=0.3)
    visualization.save_plot("Age Distribution")
    
    # Plot 2: Gender Distribution
    plt.figure(figsize=(8, 6))
    gender_counts = df_demo['gender'].value_counts()
    plt.pie(gender_counts.values, labels=gender_counts.index, autopct='%1.1f%%', startangle=90)
    plt.title('Gender Distribution')
    visualization.save_plot("Gender Distribution")
    
    # Read medical history if available
    if visualization.exists('patients/medical_history.csv'):
        with visualization.open('patients/medical_history.csv', 'r') as f:
            df_medical = pd.read_csv(f)
        
        # Merge datasets
        df_merged = pd.merge(df_demo, df_medical, on='patient_id')
        
        # Plot 3: BMI by Study Group
        plt.figure(figsize=(10, 6))
        study_groups = df_merged['study_group'].unique()
        for group in study_groups:
            group_data = df_merged[df_merged['study_group'] == group]
            plt.hist(group_data['bmi'], alpha=0.6, label=group, bins=10)
        plt.title('BMI Distribution by Study Group')
        plt.xlabel('BMI')
        plt.ylabel('Frequency')
        plt.legend()
        plt.grid(True, alpha=0.3)
        visualization.save_plot("BMIs by Study Group")
        
        # Plot 4: Age vs BMI Scatter Plot
        plt.figure(figsize=(10, 6))
        colors = {'treatment': 'red', 'control': 'blue'}
        for group in study_groups:
            group_data = df_merged[df_merged['study_group'] == group]
            plt.scatter(group_data['age'], group_data['bmi'], 
                       c=colors.get(group, 'gray'), label=group, alpha=0.6)
        plt.title('Age vs BMI by Study Group')
        plt.xlabel('Age')
        plt.ylabel('BMI')
        plt.legend()
        plt.grid(True, alpha=0.3)
        visualization.save_plot("Age vs BMI")

except Exception as e:
    print(f"Error creating visualizations: {e}")